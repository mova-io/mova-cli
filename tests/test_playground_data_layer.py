"""``movate.playground.app._build_data_layer`` — graceful degrade.

The playground's history sidebar is backed by Chainlit's
``SQLAlchemyDataLayer``, which needs ``sqlalchemy`` (+ ``greenlet``) — both
declared in the ``[playground]`` extra. Two guarantees this module keeps:

1. **The docstring's promise is real.** ``_build_data_layer`` claims it
   degrades to *no persistence* if the data-layer deps aren't importable,
   rather than crashing the whole UI. We assert that an ``ImportError`` /
   ``ModuleNotFoundError`` from the ``chainlit.data.sql_alchemy`` import is
   caught, logged at WARNING, and the function returns ``None`` (which
   Chainlit treats as "no data layer" — every caller guards with
   ``if get_data_layer():``).
2. **Happy path still builds a layer.** When the deps ARE present, the
   default SQLite path returns a real ``SQLAlchemyDataLayer``.

These tests require the ``[playground]`` extra (``app.py`` imports chainlit
at module scope by design), so they ``importorskip`` chainlit — on a
no-extras install they're skipped, mirroring the CLI's install gate.
"""

from __future__ import annotations

import builtins
import importlib
import logging
import sys
from pathlib import Path

import pytest

# app.py imports chainlit at module scope (intentional, for a clear error).
# Skip the whole module on a no-extras install instead of erroring at import.
pytest.importorskip("chainlit")

# Safe to import at module scope now that chainlit is confirmed present.
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

pytestmark = pytest.mark.unit


def _reload_app_with_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Import ``movate.playground.app`` with history enabled + a temp DB dir.

    ``_DATA_LAYER_CFG`` is resolved at import time from env, so we set the
    env first and (re)import the module fresh. History defaults ON; we
    point the SQLite path at ``tmp_path`` so no real ``~/.mdk`` dir is
    touched.
    """
    monkeypatch.delenv("MDK_PLAYGROUND_NO_HISTORY", raising=False)
    monkeypatch.delenv("MDK_PLAYGROUND_THREADS_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    sys.modules.pop("movate.playground.app", None)
    app = importlib.import_module("movate.playground.app")
    return app


def test_build_data_layer_returns_none_when_sqlalchemy_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing data-layer dep → warn + return None (no crash)."""
    app = _reload_app_with_history(monkeypatch, tmp_path)

    # History must be enabled for the data-layer fn to exist at all.
    assert app._DATA_LAYER_CFG.enabled is True
    assert hasattr(app, "_build_data_layer")

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "chainlit.data.sql_alchemy":
            raise ModuleNotFoundError("No module named 'sqlalchemy'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with caplog.at_level(logging.WARNING, logger=app.logger.name):
        result = app._build_data_layer()

    assert result is None, "expected graceful degrade to None on missing dep"
    assert any(
        rec.levelno == logging.WARNING and "history disabled" in rec.getMessage().lower()
        for rec in caplog.records
    ), "expected a WARNING log explaining the degrade"


def test_build_data_layer_builds_when_deps_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deps present → a real SQLAlchemyDataLayer is returned (SQLite default)."""
    app = _reload_app_with_history(monkeypatch, tmp_path)
    result = app._build_data_layer()

    assert isinstance(result, SQLAlchemyDataLayer)
    # The SQLite parent dir is created as a side effect (path under tmp HOME).
    assert app._DATA_LAYER_CFG.sqlite_path is not None
    assert app._DATA_LAYER_CFG.sqlite_path.parent.is_dir()


def test_no_history_skips_data_layer_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--no-history → the data layer is never registered, so it can't crash."""
    monkeypatch.setenv("MDK_PLAYGROUND_NO_HISTORY", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    sys.modules.pop("movate.playground.app", None)
    app = importlib.import_module("movate.playground.app")

    assert app._DATA_LAYER_CFG.enabled is False
    # The decorated builder only exists inside the `if cfg.enabled:` block.
    assert not hasattr(app, "_build_data_layer")
