"""Pure-logic tests for multi-runtime target resolution — NO Chainlit.

The playground's multi-target mode (one chat profile per configured
runtime) is built on :mod:`movate.playground.targets`, which is pure:
config → :class:`PlaygroundTarget` records + the JSON env hand-off
between the CLI launcher and the Chainlit app. These tests cover:

* config → profiles mapping (incl. per-target key resolution + sort),
* missing-key → kept-but-disabled (graceful degrade, never dropped),
* the encode/decode env round-trip (the launcher→app contract),
* malformed-hand-off tolerance (defense in depth),
* the profile label / description shapes the UI renders.

The module imports WITHOUT ``chainlit`` (the whole point — it's
unit-testable on a no-extras install); we assert that import hygiene too.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass

import pytest

from movate.playground.targets import (
    TARGETS_ENV_VAR,
    PlaygroundTarget,
    decode_targets,
    encode_targets,
    resolve_targets_from_config,
)

pytestmark = pytest.mark.unit


@dataclass
class _FakeTarget:
    """Stand-in for ``TargetConfig`` (duck-typed: .url / .key_env)."""

    url: str
    key_env: str


# ---------------------------------------------------------------------------
# Import hygiene — the targets module must NOT require chainlit.
# ---------------------------------------------------------------------------


def test_targets_module_imports_without_chainlit() -> None:
    """A no-extras install must still import the pure targets module."""
    assert importlib.util.find_spec("movate.playground.targets") is not None
    import movate.playground.targets as mod  # noqa: PLC0415

    # No chainlit attribute leaked in.
    assert not hasattr(mod, "cl")


# ---------------------------------------------------------------------------
# config → profiles mapping
# ---------------------------------------------------------------------------


def test_resolve_maps_each_target_and_resolves_keys() -> None:
    """One PlaygroundTarget per config target; keys read from key_env."""
    cfg = {
        "prod": _FakeTarget(url="https://prod.example", key_env="MDK_PROD_KEY"),
        "dev": _FakeTarget(url="http://127.0.0.1:8000", key_env="MDK_DEV_KEY"),
    }
    env = {"MDK_PROD_KEY": "prod-token", "MDK_DEV_KEY": "dev-token"}

    targets = resolve_targets_from_config(cfg, env=env)

    # Sorted by name for a stable picker order.
    assert [t.name for t in targets] == ["dev", "prod"]
    by_name = {t.name: t for t in targets}
    assert by_name["prod"].url == "https://prod.example"
    assert by_name["prod"].api_key == "prod-token"
    assert by_name["prod"].key_available is True
    assert by_name["dev"].api_key == "dev-token"


def test_resolve_keeps_target_with_missing_key_disabled() -> None:
    """A target whose key env var is unset is KEPT but flagged unavailable."""
    cfg = {
        "dev": _FakeTarget(url="http://dev", key_env="MDK_DEV_KEY"),
        "prod": _FakeTarget(url="https://prod", key_env="MDK_PROD_KEY"),
    }
    env = {"MDK_DEV_KEY": "dev-token"}  # prod key absent

    targets = resolve_targets_from_config(cfg, env=env)
    by_name = {t.name: t for t in targets}

    # Both targets present — the missing-key one is not dropped.
    assert set(by_name) == {"dev", "prod"}
    assert by_name["prod"].key_available is False
    assert by_name["prod"].api_key is None
    assert by_name["dev"].key_available is True


def test_resolve_treats_empty_key_as_missing() -> None:
    """An env var set to whitespace/empty counts as no key."""
    cfg = {"dev": _FakeTarget(url="http://dev", key_env="MDK_DEV_KEY")}
    targets = resolve_targets_from_config(cfg, env={"MDK_DEV_KEY": "   "})
    assert targets[0].key_available is False


def test_resolve_skips_target_without_url() -> None:
    """A malformed target with no URL can't be talked to → skipped."""
    cfg = {
        "ok": _FakeTarget(url="http://ok", key_env="MDK_OK_KEY"),
        "broken": _FakeTarget(url="", key_env="MDK_BROKEN_KEY"),
    }
    targets = resolve_targets_from_config(cfg, env={})
    assert [t.name for t in targets] == ["ok"]


def test_resolve_empty_config_yields_empty_list() -> None:
    """No configured targets → empty list → single-runtime mode upstream."""
    assert resolve_targets_from_config({}, env={}) == []


# ---------------------------------------------------------------------------
# encode/decode env hand-off (launcher → app contract)
# ---------------------------------------------------------------------------


def test_encode_decode_round_trip_preserves_fields() -> None:
    """The JSON hand-off preserves every field incl. the resolved key."""
    targets = [
        PlaygroundTarget(name="dev", url="http://dev", key_env="MDK_DEV_KEY", api_key="k1"),
        PlaygroundTarget(name="prod", url="https://prod", key_env="MDK_PROD_KEY", api_key=None),
    ]
    decoded = decode_targets(encode_targets(targets))
    assert decoded == targets
    assert decoded[1].api_key is None
    assert decoded[1].key_available is False


def test_encode_empty_is_empty_string() -> None:
    """No targets → empty string so the launcher can skip the env var."""
    assert encode_targets([]) == ""


@pytest.mark.parametrize("raw", [None, "", "not-json", "{}", "[1, 2, 3]", "null"])
def test_decode_tolerates_garbage(raw: str | None) -> None:
    """Unset / empty / malformed hand-off → empty list (fall back to single)."""
    assert decode_targets(raw) == []


def test_targets_env_var_name_is_stable() -> None:
    """The env var name is a contract between launcher + app — pin it."""
    assert TARGETS_ENV_VAR == "MDK_PLAYGROUND_TARGETS"


# ---------------------------------------------------------------------------
# UI label / description shapes
# ---------------------------------------------------------------------------


def test_profile_label_is_name_plus_url() -> None:
    t = PlaygroundTarget(name="prod", url="https://prod", key_env="MDK_PROD_KEY", api_key="k")
    assert t.profile_label() == "prod (https://prod)"


def test_profile_description_flags_missing_key() -> None:
    """The picker blurb must call out a missing key (with the env var name)."""
    ok = PlaygroundTarget(name="dev", url="http://dev", key_env="MDK_DEV_KEY", api_key="k")
    missing = PlaygroundTarget(name="prod", url="https://prod", key_env="MDK_PROD_KEY")
    assert "MDK_DEV_KEY" in ok.profile_description()
    assert "no key" in missing.profile_description().lower()
    assert "MDK_PROD_KEY" in missing.profile_description()


def test_to_dict_from_dict_inverse() -> None:
    t = PlaygroundTarget(name="dev", url="http://dev", key_env="MDK_DEV_KEY", api_key="tok")
    assert PlaygroundTarget.from_dict(t.to_dict()) == t


def test_from_dict_tolerates_missing_optional_api_key() -> None:
    t = PlaygroundTarget.from_dict({"name": "dev", "url": "http://dev", "key_env": "MDK_DEV_KEY"})
    assert t.api_key is None
    assert t.key_available is False
