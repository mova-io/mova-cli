"""Symmetric encryption for per-user Movate API keys stored by the bot.

Slice 3.1.c stores users' Movate API keys at rest so the bot can
substitute the user's key for the fleet key on every \`run\`. Plaintext
is unacceptable even for alpha — a bot DB dump must not give an
attacker working credentials for every connected user.

Design choices
--------------

* **Fernet (AES-128-CBC + HMAC-SHA256) via the ``cryptography``
  package.** Standard primitive; well-vetted; rotation-friendly via
  ``MultiFernet`` if/when we add key rotation.
* **Key from env**, not from disk. ``MOVATE_TEAMS_ENCRYPTION_KEY`` is
  the canonical source. Operators set it once at bot startup; the bot
  fails loud at boot if it's missing (rather than crashing later on
  first key lookup).
* **No KMS yet.** Production deployment in Movate Azure should swap
  this for a Key Vault-backed key derivation, but that's a hardening
  PR. The interface here (``encrypt`` / ``decrypt`` taking bytes) is
  KMS-shaped — drop-in replacement when the Azure migration lands.
* **Hint exposed separately.** The store also keeps the last 4 chars
  of every key in plaintext as an ``api_key_hint`` so ``whoami`` can
  show ``...AbCd`` without ever decrypting. This is intentional
  human-affordance, not a security boundary — the hint alone can't
  be used to forge a key.
"""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cryptography.fernet import Fernet

# Env var name centralised so the CLI, app, and crypto module agree.
ENV_ENCRYPTION_KEY = "MOVATE_TEAMS_ENCRYPTION_KEY"


class TeamsCryptoError(Exception):
    """Raised when encryption / decryption / key resolution fails.

    Includes the configuration-error subclass for missing env.
    Operators should catch this at startup so the bot doesn't appear
    healthy but fail silently on first ``connect``."""


class MissingEncryptionKeyError(TeamsCryptoError):
    """Raised when ``MOVATE_TEAMS_ENCRYPTION_KEY`` is unset.

    Distinct subclass so the CLI can render a different help message
    (``set the env var``) vs. a generic crypto failure (``rotation
    error``, etc.).
    """


def get_fernet(*, key_override: bytes | str | None = None) -> Fernet:
    """Build a :class:`Fernet` from the configured encryption key.

    Args:
        key_override: Tests pass an explicit key to avoid env coupling.
            Production leaves it ``None`` so the env var is read.

    Returns the Fernet instance ready for ``encrypt`` / ``decrypt``.

    Raises:
        MissingEncryptionKeyError: if no override AND env is empty.
        TeamsCryptoError: if the key isn't a valid Fernet key
            (32 url-safe-b64-encoded bytes).
    """
    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415
    except ImportError as exc:
        raise TeamsCryptoError(
            "the 'cryptography' package is required for Teams identity "
            "binding. Install with: uv add 'movate-cli[teams]'"
        ) from exc

    raw = key_override if key_override is not None else os.environ.get(ENV_ENCRYPTION_KEY)
    if not raw:
        raise MissingEncryptionKeyError(
            f"{ENV_ENCRYPTION_KEY} is not set. Generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` and export it '
            "before starting the bot."
        )

    key_bytes = raw if isinstance(raw, bytes) else raw.encode("ascii")
    try:
        return Fernet(key_bytes)
    except (ValueError, TypeError) as exc:
        # Fernet raises ValueError on a malformed key (wrong length /
        # not url-safe-base64). Translate to our taxonomy so callers
        # can react uniformly.
        raise TeamsCryptoError(
            f"{ENV_ENCRYPTION_KEY} is set but isn't a valid Fernet key "
            "(must be 32 url-safe-base64-encoded bytes)."
        ) from exc


def encrypt_key(plaintext: str, *, fernet: Fernet | None = None) -> bytes:
    """Encrypt a Movate API key for storage.

    ``fernet`` is injectable for tests; production callers pass
    ``None`` and we resolve via :func:`get_fernet`.

    Returns the ciphertext as raw bytes (sqlite BLOB-safe).
    """
    f = fernet or get_fernet()
    return f.encrypt(plaintext.encode("utf-8"))


def decrypt_key(ciphertext: bytes, *, fernet: Fernet | None = None) -> str:
    """Decrypt a stored API key back to its plaintext form.

    Used by :class:`IdentityResolver` when binding a user to a fresh
    :class:`MovateClient`. Raises :class:`TeamsCryptoError` if the
    ciphertext was produced with a different key (rotation drift, env
    misconfig) — the resolver surfaces this as "key rotation broke
    your binding; please /movate connect again".
    """
    f = fernet or get_fernet()
    try:
        return f.decrypt(ciphertext).decode("utf-8")
    except Exception as exc:
        raise TeamsCryptoError(
            "couldn't decrypt the stored API key. The "
            f"{ENV_ENCRYPTION_KEY} may have rotated; the user should "
            "rebind via DM `/movate connect <new-api-key>`."
        ) from exc


# Number of trailing chars rendered in the public ``key_hint`` — the
# bit users see in ``whoami`` (``...AbCd``). Long enough to disambiguate
# common rotations, short enough to leak negligible entropy.
_KEY_HINT_LEN = 4


def hint_from_key(plaintext: str) -> str:
    """Last 4 chars of an API key — safe to display in cards / logs.

    Lets ``whoami`` show ``...AbCd`` without ever decrypting the
    stored ciphertext, AND helps users spot when they're bound to
    the wrong key (different ``...XxYy`` than what they expected).
    """
    if len(plaintext) <= _KEY_HINT_LEN:
        # Pathological case; just return the whole thing. The store's
        # input validation should keep this from happening (api keys
        # have a fixed minimum length).
        return plaintext
    return plaintext[-_KEY_HINT_LEN:]


def generate_dev_key() -> bytes:
    """Mint a fresh Fernet key for local dev / first-run.

    NOT for production — operators should store the key in a secret
    manager and inject via env. This helper exists so `mdk teams-bot
    serve` can print a one-liner hint when the env var is missing.
    """
    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415
    except ImportError as exc:
        raise TeamsCryptoError("'cryptography' not installed; can't generate a key.") from exc
    return Fernet.generate_key()


def _looks_like_fernet_key(value: str) -> bool:
    """Cheap sanity check: 44 chars of url-safe-b64 = 32 raw bytes.

    Used in error messages so we can say "set MOVATE_TEAMS_ENCRYPTION_KEY
    — looks like you set MOVATE_TEAMS_FLEET_API_KEY instead" rather
    than a blunt parse error.
    """
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception:
        return False
    return len(decoded) == 32  # noqa: PLR2004
