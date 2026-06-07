"""Voice audio privacy / retention policy enforcement (backlog #215).

Enterprise voice deployments must guarantee that raw audio bytes are:

1. **NEVER logged** — no ``logger.debug(audio_data)`` anywhere in the voice
   pipeline.  This module defines the policy; the codebase audit confirming no
   raw audio in logs is part of the PR gate.
2. **Retained only as long as the configured policy allows** — governed by the
   ``VOICE_AUDIO_RETENTION`` env var (default ``"ephemeral"``).
3. **Surfaced in the capabilities response** so clients know the server's
   privacy posture.

Retention policies
------------------

+-------------+--------------------------------------------------------------+
| Value       | Behavior                                                     |
+=============+==============================================================+
| ``ephemeral``| Audio discarded immediately after the turn completes         |
|             | (default — the safest posture).                              |
+-------------+--------------------------------------------------------------+
| ``session`` | Audio kept in memory for the session duration (enables       |
|             | replay for debugging / QA), purged on session close.         |
+-------------+--------------------------------------------------------------+
| ``none``    | Audio is never stored at any point — even the per-turn       |
|             | replay buffer used by failover is cleared after each         |
|             | provider attempt.                                            |
+-------------+--------------------------------------------------------------+

This module extends :mod:`movate.voice.pii` (which handles *transcript* PII)
to cover the *audio* surface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

RetentionPolicy = Literal["ephemeral", "session", "none"]

# The env var the operator sets to control audio retention.
RETENTION_ENV_VAR = "VOICE_AUDIO_RETENTION"
_VALID_POLICIES: frozenset[str] = frozenset({"ephemeral", "session", "none"})
_DEFAULT_POLICY: RetentionPolicy = "ephemeral"


def get_retention_policy() -> RetentionPolicy:
    """Read the configured retention policy from the environment.

    Falls back to ``"ephemeral"`` when the env var is unset or has an invalid
    value (with a warning to stderr on invalid).
    """
    raw = os.environ.get(RETENTION_ENV_VAR, "").strip().lower()
    if not raw:
        return _DEFAULT_POLICY
    if raw in _VALID_POLICIES:
        return raw  # type: ignore[return-value]
    import sys  # noqa: PLC0415

    print(
        f"[voice/privacy] ignoring invalid {RETENTION_ENV_VAR}={raw!r}; "
        f"valid: {sorted(_VALID_POLICIES)}; defaulting to {_DEFAULT_POLICY!r}",
        file=sys.stderr,
    )
    return _DEFAULT_POLICY


@dataclass
class AudioRetentionManager:
    """Enforces the per-session audio retention policy.

    The pipeline / transport creates one per session and calls
    :meth:`record_turn_audio` after each turn.  The manager decides whether to
    keep or discard the audio bytes based on the policy.

    Under ``"none"`` the manager never stores anything.
    Under ``"ephemeral"`` it discards after each turn.
    Under ``"session"`` it keeps until :meth:`purge` (session close).
    """

    policy: RetentionPolicy = field(default_factory=get_retention_policy)
    _session_audio: list[bytes] = field(default_factory=list, init=False, repr=False)
    _turn_count: int = field(default=0, init=False)
    _bytes_discarded: int = field(default=0, init=False)

    def record_turn_audio(self, audio_bytes: bytes) -> None:
        """Record a turn's audio under the current retention policy."""
        self._turn_count += 1
        if self.policy == "none":
            self._bytes_discarded += len(audio_bytes)
            return
        if self.policy == "ephemeral":
            # Under ephemeral, we accept but immediately discard.
            self._bytes_discarded += len(audio_bytes)
            return
        # "session" — keep for the session duration.
        self._session_audio.append(audio_bytes)

    def get_session_audio(self) -> list[bytes]:
        """Return retained audio (only non-empty under ``"session"`` policy)."""
        return list(self._session_audio)

    def purge(self) -> int:
        """Purge all retained audio. Returns the number of bytes purged."""
        total = sum(len(b) for b in self._session_audio)
        self._session_audio.clear()
        self._bytes_discarded += total
        return total

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def bytes_discarded(self) -> int:
        return self._bytes_discarded

    def stats(self) -> dict[str, Any]:
        """Return privacy stats for observability (no audio content)."""
        return {
            "policy": self.policy,
            "turns_recorded": self._turn_count,
            "bytes_retained": sum(len(b) for b in self._session_audio),
            "bytes_discarded": self._bytes_discarded,
        }


def privacy_capabilities(
    *,
    pii_redaction_enabled: bool = False,
) -> dict[str, Any]:
    """Build the ``voice.privacy`` section for a capabilities response.

    Called by the transport's capabilities endpoint so clients know what
    privacy controls are active.
    """
    policy = get_retention_policy()
    return {
        "retention_policy": policy,
        "pii_redaction_enabled": pii_redaction_enabled,
        "audio_logging": "never",
        "supported_policies": sorted(_VALID_POLICIES),
    }


# Thresholds for the log-audit heuristic.
_MIN_AUDIO_BYTES = 64  # bytes shorter than this are probably test tags, not audio
_MIN_REPR_LEN = 128  # repr text shorter than this is not a large bytes literal
_MIN_HEX_ESCAPES = 16  # audio bytes produce many \\x escapes


def assert_no_audio_in_log_record(record: Any) -> bool:
    """Test helper: check a logging record doesn't contain raw audio bytes.

    Scans the formatted message and args for ``bytes`` objects longer than
    ``_MIN_AUDIO_BYTES`` bytes (a short tag like ``b'frame'`` in a test is
    fine; a real audio buffer is kilobytes). Returns ``True`` when the record
    is clean.
    """
    msg = str(getattr(record, "msg", ""))
    args = getattr(record, "args", ()) or ()

    # Check the formatted message for long byte-string representations.
    if _looks_like_audio(msg):
        return False

    # Check each positional arg.
    for arg in args if isinstance(args, tuple) else (args,):
        if isinstance(arg, (bytes, bytearray)) and len(arg) > _MIN_AUDIO_BYTES:
            return False
        if _looks_like_audio(str(arg)):
            return False
    return True


def _looks_like_audio(text: str) -> bool:
    """Heuristic: does ``text`` contain a representation of a large bytes literal?"""
    return (
        len(text) > _MIN_REPR_LEN
        and ("\\x" in text or "b'" in text)
        and text.count("\\x") > _MIN_HEX_ESCAPES
    )
