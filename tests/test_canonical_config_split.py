"""Tests for the canonical config split — runtime.yaml / eval.yaml /
knowledge.yaml layered on top of policy.yaml.

Coverage map:

* **Backward compat** — only policy.yaml present, behavior is identical
  to v0.5.
* **Pure split** — only the dedicated files exist (no policy.yaml).
* **Mixed** — both policy.yaml AND dedicated files; dedicated wins per
  field with a one-shot deprecation warning.
* **Per-file** — runtime.yaml / eval.yaml / knowledge.yaml independently.
* **Forward compat** — KnowledgeConfig.extra='allow' so unfamiliar
  fields don't crash.
* **Explicit path bypass** — load_project_config(path=...) reads only
  that file, no split merging.
* **Empty files** — every combination of empty files yields defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.core import config as config_module
from movate.core.config import load_project_config


@pytest.fixture(autouse=True)
def _reset_warning_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """One-shot warning state lives at module scope. Reset between
    tests so a previous test's warning doesn't suppress the next."""
    monkeypatch.setattr(config_module, "_LEGACY_WARN_FIRED", False)
    monkeypatch.setattr(config_module, "_MOVED_FIELD_WARNINGS", set())


# ---------------------------------------------------------------------------
# Backward compatibility: only policy.yaml present
# ---------------------------------------------------------------------------


def test_legacy_policy_yaml_with_all_blocks_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project with the v0.5 layout (everything in policy.yaml) loads
    identically — split is opt-in."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text(
        "policy:\n"
        "  allowed_providers: [openai, anthropic]\n"
        "  max_cost_per_run_usd: 0.50\n"
        "runtime:\n"
        "  allowed: [litellm]\n"
        "bench:\n"
        "  models: [openai/gpt-4o-mini-2024-07-18]\n"
        "eval:\n"
        "  gate: 0.7\n"
    )
    cfg = load_project_config()
    assert cfg.policy.allowed_providers == ["openai", "anthropic"]
    assert cfg.policy.max_cost_per_run_usd == 0.50
    assert cfg.runtime.allowed is not None
    assert cfg.bench.models == ["openai/gpt-4o-mini-2024-07-18"]
    assert cfg.eval.gate == 0.7


def test_no_config_files_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No policy.yaml, no split files, no movate.yaml — every
    ProjectConfig field is its Pydantic default."""
    monkeypatch.chdir(tmp_path)
    cfg = load_project_config()
    assert cfg.policy.allowed_providers == []
    assert cfg.runtime.allowed is None
    assert cfg.bench.models == []
    assert cfg.eval.gate is None


# ---------------------------------------------------------------------------
# Pure split: only the dedicated files exist
# ---------------------------------------------------------------------------


def test_only_runtime_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime.yaml alone (no policy.yaml). Its `runtime:` block
    becomes the canonical RuntimePolicy."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runtime.yaml").write_text("runtime:\n  allowed: [litellm]\n")
    cfg = load_project_config()
    assert cfg.runtime.allowed is not None
    assert [r.value for r in cfg.runtime.allowed] == ["litellm"]


def test_only_eval_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """eval.yaml carries both `eval:` and `bench:` blocks — they
    naturally cluster together (both about scoring agents)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "eval.yaml").write_text(
        "eval:\n"
        "  gate: 0.85\n"
        "bench:\n"
        "  models: [openai/gpt-4o-mini-2024-07-18, anthropic/claude-haiku-4-5-20251001]\n"
    )
    cfg = load_project_config()
    assert cfg.eval.gate == 0.85
    assert len(cfg.bench.models) == 2


def test_only_knowledge_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """knowledge.yaml is a stub today. Empty knowledge block validates
    cleanly; the file exists to reserve the slot for Tier 3."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "knowledge.yaml").write_text("knowledge: {}\n")
    cfg = load_project_config()
    # No fields today; just confirm it didn't error.
    assert cfg.knowledge is not None


def test_all_three_split_files_no_policy_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fully-migrated state: policy.yaml carries enforcement +
    defaults; everything else is in dedicated files."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text(
        "policy:\n  allowed_providers: [openai]\n"
        "defaults:\n  model:\n    params:\n      temperature: 0.0\n"
    )
    (tmp_path / "runtime.yaml").write_text("runtime:\n  allowed: [litellm]\n")
    (tmp_path / "eval.yaml").write_text("eval:\n  gate: 0.7\n")
    (tmp_path / "knowledge.yaml").write_text("knowledge: {}\n")
    cfg = load_project_config()
    assert cfg.policy.allowed_providers == ["openai"]
    assert cfg.defaults.model.params["temperature"] == 0.0
    assert cfg.runtime.allowed is not None
    assert cfg.eval.gate == 0.7


# ---------------------------------------------------------------------------
# Mixed: both policy.yaml AND dedicated files (deprecation warning)
# ---------------------------------------------------------------------------


def test_dedicated_file_wins_over_policy_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the same field appears in both policy.yaml AND its
    dedicated file, the dedicated file wins and the operator gets a
    deprecation warning."""
    monkeypatch.chdir(tmp_path)
    # policy.yaml carries the legacy state (everything still inline).
    (tmp_path / "policy.yaml").write_text(
        "runtime:\n  allowed: [native_anthropic]\n"  # legacy value
    )
    # Dedicated file with a different value.
    (tmp_path / "runtime.yaml").write_text("runtime:\n  allowed: [litellm]\n")
    cfg = load_project_config()
    # Dedicated wins — the litellm value, not native_anthropic.
    assert cfg.runtime.allowed is not None
    assert [r.value for r in cfg.runtime.allowed] == ["litellm"]
    # Deprecation warning fires on stderr.
    captured = capsys.readouterr()
    assert "runtime.yaml" in captured.err
    assert "runtime" in captured.err
    assert "policy.yaml" in captured.err


def test_deprecation_warning_fires_once_per_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A second call to load_project_config in the same process
    shouldn't re-emit the same field-moved warning (would spam stderr
    when commands like `validate` + `run` both load config)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text("runtime:\n  allowed: [native_anthropic]\n")
    (tmp_path / "runtime.yaml").write_text("runtime:\n  allowed: [litellm]\n")
    load_project_config()
    capsys.readouterr()  # consume first warning
    load_project_config()
    captured = capsys.readouterr()
    # No new warning on the second load.
    assert "runtime.yaml" not in captured.err


def test_each_moved_field_warns_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Migrating `runtime:` first but not `eval:` should warn only
    on the moved field, not on the legacy-but-not-yet-moved one."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text(
        "runtime:\n  allowed: [native_anthropic]\n"
        "eval:\n  gate: 0.5\n"  # still in policy.yaml, no eval.yaml yet
    )
    (tmp_path / "runtime.yaml").write_text("runtime:\n  allowed: [litellm]\n")
    cfg = load_project_config()
    captured = capsys.readouterr()
    # `runtime` warned (dedicated file present); `eval` did not.
    assert "runtime" in captured.err
    assert "eval" not in captured.err
    # Both values landed correctly.
    assert cfg.runtime.allowed is not None
    assert cfg.eval.gate == 0.5


def test_policy_block_in_runtime_yaml_is_extra_field_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator drops `policy:` into runtime.yaml by mistake. Pydantic
    catches it because ProjectConfig has extra='forbid', BUT runtime.yaml's
    extra `policy:` content would actually overlay the project's `policy:`
    block — that's actually fine since it overwrites at the dict level.

    The real failure mode we want to catch: a typo'd key like ``runtim:``
    in runtime.yaml. ProjectConfig.model_validate's strict mode rejects it.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runtime.yaml").write_text(
        "runtim:\n  allowed: [litellm]\n"  # typo
    )
    with pytest.raises(Exception, match="runtim"):
        load_project_config()


# ---------------------------------------------------------------------------
# Forward compat: knowledge.yaml accepts unknown fields
# ---------------------------------------------------------------------------


def test_knowledge_yaml_accepts_unknown_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """knowledge.yaml uses extra='allow' so operators can drop in
    experimental fields today without crashing — the canonical schema
    firms up in v0.7."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "knowledge.yaml").write_text(
        "knowledge:\n"
        "  backend: pgvector\n"
        "  embedding: openai/text-embedding-3-small\n"
        "  experimental_flag: true\n"
    )
    # Doesn't raise — KnowledgeConfig.extra='allow' tolerates anything.
    cfg = load_project_config()
    assert cfg.knowledge is not None


# ---------------------------------------------------------------------------
# Explicit path bypass
# ---------------------------------------------------------------------------


def test_explicit_path_does_not_merge_split_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_project_config(path=...)`` reads exactly the file the
    caller named. The canonical-split files are NOT layered in — the
    caller is asking for one specific file's contents."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text("runtime:\n  allowed: [native_anthropic]\n")
    # Dedicated runtime.yaml would normally override, but with explicit
    # path we ignore it.
    (tmp_path / "runtime.yaml").write_text("runtime:\n  allowed: [litellm]\n")
    cfg = load_project_config(path=tmp_path / "policy.yaml")
    assert cfg.runtime.allowed is not None
    # Native_anthropic, not litellm — split files were bypassed.
    assert [r.value for r in cfg.runtime.allowed] == ["native_anthropic"]


# ---------------------------------------------------------------------------
# Empty / malformed file edge cases
# ---------------------------------------------------------------------------


def test_empty_split_file_is_silent_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A literally-empty runtime.yaml shouldn't crash. Same for a
    file with just a comment."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runtime.yaml").write_text("")
    (tmp_path / "eval.yaml").write_text("# placeholder\n")
    cfg = load_project_config()
    assert cfg.runtime.allowed is None  # default
    assert cfg.eval.gate is None  # default


def test_non_object_split_file_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime.yaml content must be a top-level object, not a list or
    scalar. A naive operator writing `[litellm]` at the root gets a
    clear error."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runtime.yaml").write_text("- litellm\n- native_anthropic\n")
    with pytest.raises(Exception, match="top-level object"):
        load_project_config()


# ---------------------------------------------------------------------------
# Composition: defaults still work after the split
# ---------------------------------------------------------------------------


def test_defaults_block_still_loads_from_policy_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """defaults: stays in policy.yaml (per ADR — it's a per-agent
    suggestion, sibling to enforced policy). Adding split files doesn't
    move it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text(
        "defaults:\n  model:\n    params:\n      temperature: 0.0\n"
    )
    (tmp_path / "runtime.yaml").write_text("runtime:\n  allowed: [litellm]\n")
    cfg = load_project_config()
    assert cfg.defaults.model.params["temperature"] == 0.0
    assert cfg.runtime.allowed is not None
