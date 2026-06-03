"""Tests for the web-demo live voice-tuning controls (ADR 071 D4 / ADR 073 D3).

The demo's ``Session`` exposes ``set_keyterms`` / ``set_endpointing`` so the
audience can A/B STT accuracy (keyterm boosting) and the endpointing
silence-wait latency floor *live*, without a redeploy. The values are passed
per-turn to ``run_voice_pipeline``. The module under test lives at
``examples/web_demo/server.py`` — outside ``src`` (CLAUDE.md rule 6: demo-level
concern), so we add the demo dir to ``sys.path`` to import it, mirroring
``test_web_demo_recording.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_DEMO_DIR = Path(__file__).resolve().parent.parent / "examples" / "web_demo"
sys.path.insert(0, str(_DEMO_DIR))

from server import Session, _demo_keyterms  # noqa: E402 - sys.path tweak above


def test_set_keyterms_from_csv() -> None:
    """A comma/newline string becomes a cleaned keyterm list."""
    s = Session()
    assert s.set_keyterms("VPN, Okta\nMova-iO") == {"keyterms": ["VPN", "Okta", "Mova-iO"]}
    assert s.keyterms == ["VPN", "Okta", "Mova-iO"]


def test_set_keyterms_from_list() -> None:
    s = Session()
    assert s.set_keyterms(["SSO", "  MFA  ", ""]) == {"keyterms": ["SSO", "MFA"]}


def test_set_keyterms_empty_resets_to_demo_default() -> None:
    """Empty input restores the curated demo vocabulary (not a blank list)."""
    s = Session()
    s.set_keyterms("temp")
    assert s.set_keyterms("") == {"keyterms": list(_demo_keyterms())}
    assert s.keyterms == list(_demo_keyterms())


def test_set_endpointing_clamps_and_resets() -> None:
    s = Session()
    assert s.endpointing_ms is None  # default keeps the adapter value (1500 ms)
    assert s.set_endpointing(700) == {"endpointing_ms": 700}
    assert s.set_endpointing(99_999) == {"endpointing_ms": 10_000}  # clamped
    assert s.set_endpointing(-5) == {"endpointing_ms": 0}  # clamped low
    assert s.set_endpointing(None) == {"endpointing_ms": None}  # reset
    assert s.set_endpointing("") == {"endpointing_ms": None}
    assert s.set_endpointing("not-a-number") == {"endpointing_ms": None}  # tolerant


def test_defaults_seed_demo_keyterms_and_no_endpointing_override() -> None:
    s = Session()
    assert s.keyterms == list(_demo_keyterms())
    assert s.endpointing_ms is None
