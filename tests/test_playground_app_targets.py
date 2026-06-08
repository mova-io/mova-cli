"""``movate.playground.app`` multi-target wiring — Chainlit-gated.

These exercise the Chainlit app's multi-target behavior, which binds the
pure :mod:`movate.playground.targets` logic to the UI:

1. **Chat profiles per target.** When the launcher hands targets in via
   ``MDK_PLAYGROUND_TARGETS``, the app registers one ``cl.ChatProfile``
   per target (and registers none in single-runtime mode).
2. **Per-target client construction.** The session client is built from
   the SELECTED profile's target (its URL + its own resolved key), not a
   global key.
3. **Graceful missing-key degrade.** A selected target with no resolvable
   key must not fire a doomed request — ``start()`` short-circuits to a
   friendly hint.

``app.py`` imports chainlit at module scope (intentional), and ``_TARGETS``
is resolved at import time, so we set the env var then re-import the module
fresh — mirroring how the child ``chainlit run`` process boots.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest

pytest.importorskip("chainlit")

from movate.playground.targets import (
    TARGETS_ENV_VAR,
    PlaygroundTarget,
    encode_targets,
)

pytestmark = pytest.mark.unit


def _reload_app(monkeypatch: pytest.MonkeyPatch, targets: list[PlaygroundTarget]) -> ModuleType:
    """(Re)import ``movate.playground.app`` with ``targets`` in the env.

    History is disabled so the data-layer block (which needs a DB) stays
    out of the way; we only care about the target-mode wiring here.
    """
    monkeypatch.setenv("MDK_PLAYGROUND_NO_HISTORY", "1")
    if targets:
        monkeypatch.setenv(TARGETS_ENV_VAR, encode_targets(targets))
    else:
        monkeypatch.delenv(TARGETS_ENV_VAR, raising=False)
    sys.modules.pop("movate.playground.app", None)
    return importlib.import_module("movate.playground.app")


_DEV = PlaygroundTarget(name="dev", url="http://dev:8000", key_env="MDK_DEV_KEY", api_key="dev-tok")
_PROD = PlaygroundTarget(name="prod", url="https://prod", key_env="MDK_PROD_KEY", api_key=None)


def test_targets_decoded_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app decodes the launcher's target hand-off at import."""
    app = _reload_app(monkeypatch, [_DEV, _PROD])
    assert [t.name for t in app._TARGETS] == ["dev", "prod"]
    assert app._targets_by_name()["dev"].api_key == "dev-tok"


def test_no_targets_means_single_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent target env → single-runtime mode (empty _TARGETS)."""
    app = _reload_app(monkeypatch, [])
    assert app._TARGETS == []
    # set_chat_profiles is only wired when targets exist — the decorated
    # helper must not be defined in single-runtime mode.
    assert not hasattr(app, "_chat_profiles")


@pytest.mark.asyncio
async def test_chat_profiles_one_per_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-target mode registers one ChatProfile per configured target."""
    app = _reload_app(monkeypatch, [_DEV, _PROD])
    assert hasattr(app, "_chat_profiles")
    profiles = await app._chat_profiles(None)
    assert [p.name for p in profiles] == ["dev", "prod"]
    # The missing-key target's description flags the absent key up front.
    prod_profile = next(p for p in profiles if p.name == "prod")
    assert "no key" in prod_profile.markdown_description.lower()


def test_selected_target_maps_profile_to_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """The selected chat profile name resolves back to its target."""
    app = _reload_app(monkeypatch, [_DEV, _PROD])

    selected = {"chat_profile": "prod"}
    monkeypatch.setattr(app.cl.user_session, "get", lambda key, default=None: selected.get(key))
    target = app._selected_target()
    assert target is not None
    assert target.name == "prod"
    assert target.key_available is False


def test_selected_target_none_in_single_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """No configured targets → _selected_target is always None."""
    app = _reload_app(monkeypatch, [])
    assert app._selected_target() is None


def test_client_from_target_uses_target_url_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-target client carries THAT target's URL + its own key, not a global."""
    app = _reload_app(monkeypatch, [_DEV, _PROD])
    client = app._client_from_target(_DEV)
    cfg = client._config
    assert cfg.runtime_url == "http://dev:8000"
    assert cfg.api_key == "dev-tok"


@pytest.mark.asyncio
async def test_start_short_circuits_on_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting a key-less target shows a friendly hint, fires NO request."""
    app = _reload_app(monkeypatch, [_DEV, _PROD])

    selected = {"chat_profile": "prod"}  # prod has no key
    monkeypatch.setattr(app.cl.user_session, "get", lambda key, default=None: selected.get(key))

    sent: list[str] = []

    class _Msg:
        def __init__(self, content: str = "", **_: object) -> None:
            self.content = content

        async def send(self) -> None:
            sent.append(self.content)

    monkeypatch.setattr(app.cl, "Message", _Msg)

    # If _init_session were reached it would build a client + hit the
    # network; assert it's NOT called so the short-circuit is real.
    def _boom() -> None:
        raise AssertionError("_init_session must not run for a key-less target")

    monkeypatch.setattr(app, "_init_session", _boom)

    await app.start()

    assert sent, "expected a friendly message"
    msg = sent[0].lower()
    assert "no key" in msg
    assert "prod" in msg
    assert "mdk_prod_key" in msg


@pytest.mark.asyncio
async def test_start_auth_error_is_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 on list_agents surfaces a 'no key for <target>' message, not a trace."""
    app = _reload_app(monkeypatch, [_DEV, _PROD])

    session: dict[str, object] = {"chat_profile": "dev"}  # dev HAS a key

    def _set(key: str, value: object) -> None:
        session[key] = value

    monkeypatch.setattr(
        app.cl.user_session, "get", lambda key, default=None: session.get(key, default)
    )
    monkeypatch.setattr(app.cl.user_session, "set", _set)

    sent: list[str] = []

    class _Msg:
        def __init__(self, content: str = "", **_: object) -> None:
            self.content = content

        async def send(self) -> None:
            sent.append(self.content)

    monkeypatch.setattr(app.cl, "Message", _Msg)

    class _Resp:
        status_code = 401

    class _AuthError(Exception):
        response = _Resp()

    class _FakeClient:
        async def get_capabilities(self) -> None:
            return None

        async def list_agents(self, **_kw: object) -> list[dict[str, object]]:
            raise _AuthError("unauthorized")

    monkeypatch.setattr(app, "_client_from_target", lambda target: _FakeClient())

    await app.start()

    assert sent, "expected a friendly auth message"
    msg = sent[0].lower()
    assert "authentication failed" in msg
    assert "dev" in msg
