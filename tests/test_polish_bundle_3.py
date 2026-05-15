"""Third polish bundle — five small UX wins.

1. ``mdk profiles use`` — echoes the transition (prior → new).
2. ``mdk snapshot list`` — relative-time "Age" column.
3. ``mdk profiles current`` — bare stdout for shell substitution.
4. ``mdk diff`` — compact A/M/D status letters (git-style).
5. ``mdk memory list --since-days`` — drop entries older than N days.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.diff_cmd import _render_row
from movate.cli.main import app
from movate.cli.snapshot_cmd import _relative_age
from movate.memory import build_memory_store
from movate.profiles.store import (
    Profile,
    ProfileRegistry,
    save_registry,
    set_active_profile,
)
from movate.snapshot import create_snapshot
from movate.snapshot.diff import FileChange
from movate.snapshot.manifest import FileEntry

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Item 1: mdk profiles use echo
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_with_two_profiles(isolated_home: Path) -> None:
    registry = ProfileRegistry()
    registry.add(Profile(name="dev"))
    registry.add(Profile(name="prod"))
    save_registry(registry)


@pytest.mark.unit
def test_profiles_use_first_time_no_prior(
    registry_with_two_profiles: None,
) -> None:
    """No prior active profile → 'active profile: dev' (no transition)."""
    result = runner.invoke(app, ["profiles", "use", "dev"])
    assert result.exit_code == 0
    assert "active profile" in result.stdout.lower()
    assert "dev" in result.stdout
    # No transition arrow on first activation
    assert "→" not in result.stdout


@pytest.mark.unit
def test_profiles_use_shows_transition(
    registry_with_two_profiles: None,
) -> None:
    """Second switch should show 'dev → prod'."""
    runner.invoke(app, ["profiles", "use", "dev"])
    result = runner.invoke(app, ["profiles", "use", "prod"])
    assert result.exit_code == 0
    assert "→" in result.stdout
    assert "dev" in result.stdout
    assert "prod" in result.stdout


@pytest.mark.unit
def test_profiles_use_noop_switch_says_already_active(
    registry_with_two_profiles: None,
) -> None:
    """Switching to the already-active profile shouldn't claim a transition."""
    runner.invoke(app, ["profiles", "use", "dev"])
    result = runner.invoke(app, ["profiles", "use", "dev"])
    assert result.exit_code == 0
    assert "already active" in result.stdout.lower()
    assert "→" not in result.stdout


# ---------------------------------------------------------------------------
# Item 2: mdk snapshot list Age column
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRelativeAge:
    def test_seconds_ago(self) -> None:
        ts = (datetime.now(UTC) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        out = _relative_age(ts)
        assert "s ago" in out

    def test_minutes_ago(self) -> None:
        ts = (datetime.now(UTC) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        out = _relative_age(ts)
        assert "m ago" in out

    def test_hours_ago(self) -> None:
        ts = (datetime.now(UTC) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        out = _relative_age(ts)
        assert "h ago" in out

    def test_days_ago(self) -> None:
        ts = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        out = _relative_age(ts)
        assert "d ago" in out

    def test_weeks_ago(self) -> None:
        ts = (datetime.now(UTC) - timedelta(weeks=4)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        out = _relative_age(ts)
        assert "w ago" in out

    def test_unparseable_returns_em_dash(self) -> None:
        """Corrupted manifest shouldn't blow up the list view."""
        assert _relative_age("not-an-iso-timestamp") == "—"

    def test_future_timestamp_renders_now(self) -> None:
        """Clock-skew / test-injected future shouldn't render '-3s ago'."""
        ts = (datetime.now(UTC) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        assert _relative_age(ts) == "now"


@pytest.mark.unit
def test_snapshot_list_table_includes_age_column(tmp_path: Path) -> None:
    """The list command actually surfaces the Age column header."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("api_version: movate/v1\n")
    create_snapshot(project_root=proj, description="baseline")
    result = runner.invoke(
        app, ["snapshot", "list", "--project", str(proj)], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0
    assert "Age" in result.stdout


# ---------------------------------------------------------------------------
# Item 3: mdk profiles current
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profiles_current_prints_active_name(
    registry_with_two_profiles: None,
) -> None:
    """Plain stdout — just the name + newline. Shell-substitution friendly."""
    set_active_profile("prod")
    result = runner.invoke(app, ["profiles", "current"])
    assert result.exit_code == 0
    # Plain stdout — name on its own line.
    assert result.stdout.strip() == "prod"


@pytest.mark.unit
def test_profiles_current_no_active_exits_1(
    registry_with_two_profiles: None,
) -> None:
    """No active marker → exit 1 with a hint on stderr."""
    set_active_profile(None)
    result = runner.invoke(app, ["profiles", "current"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Item 4: mdk diff A/M/D status letters
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiffRenderRow:
    def _entry(self, size: int = 100) -> FileEntry:
        return FileEntry(path="x.yaml", sha256="abc", size=size)

    def test_added_renders_a_letter(self) -> None:
        change = FileChange(path="x.yaml", kind="added", before=None, after=self._entry())
        kind, _path, _size = _render_row(change)
        assert "A" in kind
        assert "added" not in kind  # the long word is gone
        assert "[green]" in kind

    def test_removed_renders_d_letter(self) -> None:
        change = FileChange(path="x.yaml", kind="removed", before=self._entry(), after=None)
        kind, _path, _size = _render_row(change)
        assert "D" in kind
        assert "[red]" in kind

    def test_modified_renders_m_letter(self) -> None:
        change = FileChange(
            path="x.yaml",
            kind="modified",
            before=self._entry(50),
            after=self._entry(100),
        )
        kind, _path, _size = _render_row(change)
        assert "M" in kind
        assert "[yellow]" in kind


# ---------------------------------------------------------------------------
# Item 5: mdk memory list --since-days
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "memory.json"
    monkeypatch.setenv("MOVATE_MEMORY_FILE", str(target))
    monkeypatch.delenv("MOVATE_MEMORY_BACKEND", raising=False)
    return target


@pytest.mark.unit
def test_memory_list_since_days_filters_old_entries(memory_env: Path) -> None:
    """Old entry (30 days) is dropped; recent (1 hour) survives a --since-days 7 filter."""
    # Seed via the store directly so we control created_at timestamps.
    store = build_memory_store()
    asyncio.run(store.set("a", "old", {"k": 1}))
    asyncio.run(store.set("a", "new", {"k": 2}))

    # Manually rewrite the on-disk file to backdate "old".
    import json  # noqa: PLC0415

    raw = json.loads(memory_env.read_text())
    raw["a"]["old"]["created_at"] = (datetime.now(UTC) - timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    memory_env.write_text(json.dumps(raw))

    # Default list shows BOTH
    full = runner.invoke(app, ["memory", "list", "a", "--json"])
    keys_full = {e["key"] for e in __import__("json").loads(full.stdout)}
    assert keys_full == {"old", "new"}

    # --since-days 7 drops "old"
    filtered = runner.invoke(app, ["memory", "list", "a", "--since-days", "7", "--json"])
    keys_filtered = {e["key"] for e in __import__("json").loads(filtered.stdout)}
    assert keys_filtered == {"new"}


@pytest.mark.unit
def test_memory_list_since_days_zero_is_noop(memory_env: Path) -> None:
    """--since-days 0 should NOT filter (matches costs report semantics)."""
    store = build_memory_store()
    asyncio.run(store.set("a", "k1", {"v": 1}))
    result = runner.invoke(app, ["memory", "list", "a", "--since-days", "0", "--json"])
    import json  # noqa: PLC0415

    data = json.loads(result.stdout)
    assert len(data) == 1
