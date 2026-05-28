"""``mdk graph serve-dash`` — command surface, missing-extra path, security.

These tests run WITHOUT ``dash``/``dash-cytoscape`` installed (they're an
opt-in extra). What's exercised here:

* the command is registered (``mdk graph serve-dash`` exists);
* when the ``graph-dash`` extra is absent, the command prints a friendly
  install hint and exits cleanly (code 2) — no traceback, no app launch;
* ``--no-open`` is respected (browser auto-open suppressed);
* the bearer token is held server-side and never appears in any
  browser-facing / stderr output.

The live Dash app itself (``build_app``) is import-guarded behind the extra,
so its construction is monkeypatched out — these tests never import dash.
"""

from __future__ import annotations

import builtins
import json
import sys
import types

import pytest
import typer

from movate.cli import graph_dash_cmd
from movate.core.user_config import TargetConfig, UserConfigError

_FAKE_BEARER = "mvt_live_supersecret_BEARER_value_1234"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_command_is_registered() -> None:
    """``serve-dash`` is a subcommand of the ``graph`` Typer group."""
    names = {cmd.name for cmd in graph_dash_cmd.graph_app.registered_commands}
    assert "serve-dash" in names


@pytest.mark.unit
def test_graph_group_wired_into_main_app() -> None:
    """The ``graph`` group is registered on the top-level CLI app."""
    from movate.cli.main import app  # noqa: PLC0415

    group_names = {grp.name for grp in app.registered_groups}
    assert "graph" in group_names


# ---------------------------------------------------------------------------
# Missing-extra path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_extra_prints_hint_and_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """When dash isn't importable, print install hint + exit code 2."""
    real_import = builtins.__import__

    def _no_dash(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if name in {"dash", "dash_cytoscape"}:
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_dash)

    printed: list[str] = []
    monkeypatch.setattr(
        graph_dash_cmd.err,
        "print",
        lambda *a, **_: printed.append(" ".join(map(str, a))),
    )

    with pytest.raises(typer.Exit) as exc:
        graph_dash_cmd._ensure_dash_installed()

    assert exc.value.exit_code == 2
    blob = " ".join(printed)
    assert "graph-dash" in blob
    assert "not" in blob and "installed" in blob


@pytest.mark.unit
def test_serve_dash_aborts_before_resolving_target_when_extra_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extra check runs FIRST — no target resolution / app build attempted."""
    real_import = builtins.__import__

    def _no_dash(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if name in {"dash", "dash_cytoscape"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_dash)
    monkeypatch.setattr(graph_dash_cmd.err, "print", lambda *a, **k: None)

    # If these were reached, the test would fail with AssertionError instead
    # of the clean Exit.
    def _boom(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        raise AssertionError("target resolution must not run when extra missing")

    monkeypatch.setattr(graph_dash_cmd, "resolve_target", _boom)

    with pytest.raises(typer.Exit) as exc:
        graph_dash_cmd.serve_dash(target="dev")
    assert exc.value.exit_code == 2


# ---------------------------------------------------------------------------
# Happy path (dash mocked out) — --no-open + server-side bearer
# ---------------------------------------------------------------------------


class _FakeApp:
    """Stand-in for the Dash app: records run() args, never opens a socket."""

    def __init__(self) -> None:
        self.run_kwargs: dict[str, object] | None = None

    def run(self, **kwargs: object) -> None:
        self.run_kwargs = kwargs  # return immediately (don't block)


def _patch_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], dict[str, object], _FakeApp]:
    """Wire serve_dash so it runs end-to-end without dash or a real target.

    Returns (stderr_lines, build_app_kwargs, fake_app).
    """
    # 1. Pretend the extra is installed.
    monkeypatch.setattr(graph_dash_cmd, "_ensure_dash_installed", lambda: None)

    # 2. Stub target resolution + bearer.
    target_cfg = TargetConfig(url="https://runtime.example.test", key_env="MDK_X_KEY")
    monkeypatch.setattr(graph_dash_cmd, "resolve_target", lambda _n: ("prod", target_cfg))
    monkeypatch.setattr(graph_dash_cmd, "resolve_bearer_token", lambda _c: _FAKE_BEARER)
    # echo_remote_context masks the key itself; stub it to a no-op so the
    # test asserts ONLY on what serve_dash chooses to print.
    monkeypatch.setattr(graph_dash_cmd, "echo_remote_context", lambda *a, **k: None)
    monkeypatch.setattr(graph_dash_cmd, "get_global_target", lambda: None)

    # 3. Capture stderr.
    printed: list[str] = []
    monkeypatch.setattr(
        graph_dash_cmd.err,
        "print",
        lambda *a, **_: printed.append(" ".join(map(str, a))),
    )

    # 4. Stub build_app (the only thing that imports dash) + capture kwargs.
    #    serve_dash does ``from movate.cli.graph_dash_app import build_app``
    #    lazily. graph_dash_app's top-level ``import dash_cytoscape`` means we
    #    can't import the real module in a dash-less env — so inject a stub
    #    module into sys.modules. The lazy import resolves to the stub's
    #    build_app, and dash is never imported.
    build_kwargs: dict[str, object] = {}
    fake_app = _FakeApp()

    def _fake_build_app(**kwargs: object):  # type: ignore[no-untyped-def]
        build_kwargs.update(kwargs)
        return fake_app

    stub_mod = types.ModuleType("movate.cli.graph_dash_app")
    stub_mod.build_app = _fake_build_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "movate.cli.graph_dash_app", stub_mod)

    return printed, build_kwargs, fake_app


@pytest.mark.unit
def test_no_open_suppresses_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    _printed, _build_kwargs, _app = _patch_happy_path(monkeypatch)

    opened: list[str] = []

    def _record_open(url: str) -> None:
        opened.append(url)

    monkeypatch.setattr(graph_dash_cmd.webbrowser, "open", _record_open)
    # Guard: a Timer should never be scheduled under --no-open.
    timers: list[object] = []
    real_timer = graph_dash_cmd.threading.Timer
    monkeypatch.setattr(
        graph_dash_cmd.threading,
        "Timer",
        lambda *a, **k: timers.append(real_timer(*a, **k)) or timers[-1],
    )

    graph_dash_cmd.serve_dash(target="prod", no_open=True, port=8901)

    assert opened == []
    assert timers == []  # no auto-open timer scheduled


@pytest.mark.unit
def test_default_opens_browser_via_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    _printed, _build_kwargs, _app = _patch_happy_path(monkeypatch)

    scheduled: list[object] = []
    monkeypatch.setattr(
        graph_dash_cmd.threading,
        "Timer",
        lambda *a, **k: scheduled.append((a, k)) or _NoopTimer(),
    )

    graph_dash_cmd.serve_dash(target="prod", no_open=False, port=8901)

    assert len(scheduled) == 1  # one auto-open timer scheduled


class _NoopTimer:
    def start(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.mark.unit
def test_bearer_token_held_server_side(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token is passed to build_app (server-side) but never printed."""
    printed, build_kwargs, _app = _patch_happy_path(monkeypatch)
    monkeypatch.setattr(graph_dash_cmd.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(graph_dash_cmd.threading, "Timer", lambda *a, **k: _NoopTimer())

    graph_dash_cmd.serve_dash(target="prod", no_open=True, port=8901)

    # Server-side: the bearer reaches build_app as bearer_token.
    assert build_kwargs["bearer_token"] == _FAKE_BEARER

    # Browser-facing: the raw token appears in NO stderr line.
    for line in printed:
        assert _FAKE_BEARER not in line
    assert all(_FAKE_BEARER not in line for line in printed)


@pytest.mark.unit
def test_run_uses_requested_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _printed, _build_kwargs, app = _patch_happy_path(monkeypatch)
    monkeypatch.setattr(graph_dash_cmd.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(graph_dash_cmd.threading, "Timer", lambda *a, **k: _NoopTimer())

    graph_dash_cmd.serve_dash(target="prod", no_open=True, port=8901, host="127.0.0.1")

    assert app.run_kwargs is not None
    assert app.run_kwargs["host"] == "127.0.0.1"
    assert app.run_kwargs["port"] == 8901
    assert app.run_kwargs["debug"] is False


@pytest.mark.unit
def test_project_id_forwarded_to_app(monkeypatch: pytest.MonkeyPatch) -> None:
    _printed, build_kwargs, _app = _patch_happy_path(monkeypatch)
    monkeypatch.setattr(graph_dash_cmd.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(graph_dash_cmd.threading, "Timer", lambda *a, **k: _NoopTimer())

    graph_dash_cmd.serve_dash(target="prod", project="proj_123", no_open=True)

    assert build_kwargs["project_id"] == "proj_123"
    assert build_kwargs["base_url"] == "https://runtime.example.test"


# ---------------------------------------------------------------------------
# Target-resolution failure path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unresolvable_target_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph_dash_cmd, "_ensure_dash_installed", lambda: None)
    monkeypatch.setattr(graph_dash_cmd, "get_global_target", lambda: None)

    def _raise(_n: object):  # type: ignore[no-untyped-def]
        raise UserConfigError("target 'nope' not found in config. Available: (none)")

    monkeypatch.setattr(graph_dash_cmd, "resolve_target", _raise)
    errors: list[str] = []
    monkeypatch.setattr(graph_dash_cmd, "error", lambda msg, **_: errors.append(msg))

    with pytest.raises(typer.Exit) as exc:
        graph_dash_cmd.serve_dash(target="nope", no_open=True)

    assert exc.value.exit_code == 2
    assert any("not found" in e for e in errors)


# ---------------------------------------------------------------------------
# Live app smoke (only when the extra IS installed)
# ---------------------------------------------------------------------------

try:
    import dash  # noqa: F401
    import dash_cytoscape  # noqa: F401

    _DASH_AVAILABLE = True
except ImportError:
    _DASH_AVAILABLE = False


@pytest.mark.unit
@pytest.mark.skipif(not _DASH_AVAILABLE, reason="graph-dash extra not installed")
def test_build_app_does_not_leak_token_into_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With dash installed: the bearer never appears in the serialized layout.

    Skipped automatically when the opt-in extra isn't present (the gate env).
    """
    # Imported function-locally: graph_dash_app's top-level ``import dash``
    # only succeeds when the extra is present (this test is skipif-guarded).
    from movate.cli.graph_dash_app import build_app  # noqa: PLC0415

    # Stop the constructor's initial fetch from hitting the network.
    monkeypatch.setattr("httpx.get", lambda *a, **k: _raise_conn())

    app = build_app(
        base_url="https://runtime.example.test",
        bearer_token=_FAKE_BEARER,
        project_id="proj_1",
    )
    # Serialize the whole layout tree and assert the token is absent.
    layout_repr = json.dumps(_serialize(app.layout))
    assert _FAKE_BEARER not in layout_repr


def _raise_conn():  # type: ignore[no-untyped-def]
    import httpx  # noqa: PLC0415

    raise httpx.ConnectError("blocked in test")


def _serialize(component: object) -> object:
    """Best-effort recursive serialization of a Dash component tree."""
    to_plotly = getattr(component, "to_plotly_json", None)
    if callable(to_plotly):
        return _serialize(to_plotly())
    if isinstance(component, dict):
        return {k: _serialize(v) for k, v in component.items()}
    if isinstance(component, (list, tuple)):
        return [_serialize(v) for v in component]
    return str(component)
