"""``mdk playground serve`` preflight — soft-warn on Python 3.14.

On CPython 3.14 chainlit's async stack hits ``anyio.NoEventLoopError``
the first time a request reaches the UI, because sniffio 1.3.1 can't
detect the asyncio event loop. The failure is invisible at startup, so
``_warn_if_unstable_python`` surfaces a copy-paste reinstall hint up
front. It's a *soft* warning — launch still proceeds, since a future
sniffio/anyio release may add 3.14 support.
"""

from __future__ import annotations

import pytest

from movate.cli import playground


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Swap the module's stderr Console for a stub that records prints."""
    printed: list[str] = []
    monkeypatch.setattr(
        playground.err, "print", lambda *args, **_: printed.append(" ".join(map(str, args)))
    )
    return printed


@pytest.mark.unit
def test_warns_on_python_314(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playground.sys, "version_info", (3, 14, 0))
    printed = _capture(monkeypatch)

    playground._warn_if_unstable_python()

    assert len(printed) == 1
    msg = printed[0]
    assert "3.14" in msg
    assert "anyio.NoEventLoopError" in msg
    # Reinstall hint pins 3.13 and keeps the bracketed extra escaped.
    assert "uv tool install --reinstall --python 3.13" in msg
    assert "'movate-cli\\[playground]'" in msg


@pytest.mark.unit
def test_silent_on_python_313(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playground.sys, "version_info", (3, 13, 2))
    printed = _capture(monkeypatch)

    playground._warn_if_unstable_python()

    assert printed == []


@pytest.mark.unit
def test_silent_on_python_311(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playground.sys, "version_info", (3, 11, 9))
    printed = _capture(monkeypatch)

    playground._warn_if_unstable_python()

    assert printed == []
