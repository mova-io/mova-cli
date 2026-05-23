"""Read/write the user-global credentials at ``~/.movate/credentials``.

File format is the same as ``.env`` (``KEY=value`` lines), so operators
can hand-edit it with any editor. Mode 0600 — owner read/write only.

We deliberately use ``.env`` syntax rather than YAML or TOML so the
file is grep-friendly, editor-agnostic, and copy-pasteable from
:command:`env`-style snippets common in provider docs.

Backend seam (ADR 012 D2)
-------------------------

:class:`CredentialsStore` keeps the same public surface — ``read`` /
``get`` / ``set`` / ``delete`` — but now delegates raw-text load/store
to a :class:`CredentialBackend`:

* :class:`FileCredentialBackend` (default) — the historical
  ``~/.movate/credentials`` plaintext file, atomic write, mode 0600,
  comment/blank-line preservation byte-for-byte.
* :class:`KeychainCredentialBackend` (opt-in) — stores the *entire*
  credentials text blob as a SINGLE OS-keychain secret via ``keyring``
  (macOS Keychain / Windows Credential Manager / Linux Secret Service).

The keychain backend is opt-in via ``MOVATE_CRED_BACKEND=keychain``;
the file backend stays the default and the fallback. All line-editing
logic operates on an in-memory string, so both backends share identical
``.env`` semantics — only the storage substrate differs.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path
from typing import IO, Protocol

# Default location — overridable via ``MOVATE_CREDENTIALS_PATH`` for
# tests + multi-user systems. Resolves to ``~/.movate/credentials`` for
# every other invocation.
_DEFAULT_PATH = Path.home() / ".movate" / "credentials"

# Env var selecting the credential backend. ``file`` (default) keeps the
# historical plaintext store; ``keychain`` opts into the OS keychain via
# the optional ``keyring`` dependency (``mdk[keychain]``). Additive +
# default-off per ADR 012 D2.
_BACKEND_ENV = "MOVATE_CRED_BACKEND"

# Keychain coordinates: a single secret holding the whole credentials
# blob, namespaced under one service so the backend can round-trip ALL
# pairs (``keyring`` has no portable way to enumerate entries).
KEYCHAIN_SERVICE = "movate-cli"
KEYCHAIN_USERNAME = "credentials"


def _resolve_path() -> Path:
    """Resolve the credentials path, honoring the env-var override."""
    override = os.environ.get("MOVATE_CREDENTIALS_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_PATH


# Exposed for diagnostic surfaces (``mdk auth status``) that want to
# tell operators WHERE the file lives. Always evaluate via the
# function — the env var may be set after import.
CREDENTIALS_PATH = _DEFAULT_PATH


# ---------------------------------------------------------------------------
# Pure in-memory ``.env`` text manipulation
#
# These helpers operate on a single credentials text blob (string),
# decoupled from WHERE that text is stored. Both backends below supply
# the blob; these functions parse + line-edit it while preserving
# comments, blank lines, and ordering byte-for-byte.
# ---------------------------------------------------------------------------


def _parse_blob(text: str) -> dict[str, str]:
    """Return every ``KEY=value`` pair in ``text`` as a dict.

    Comments and blank lines are skipped. Lines without ``=`` are
    silently ignored — the store is operator-curated, but we tolerate
    stray junk rather than breaking on malformed entries.
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _set_in_blob(text: str, key: str, value: str) -> str:
    """Return ``text`` with one ``KEY=value`` entry inserted or updated.

    A targeted line edit — existing comments, blank lines, ordering, and
    unrelated keys are left byte-for-byte intact; only the matching key's
    line is rewritten (or, for a new key, appended at the end). This is
    what lets operators keep hand-added comments/structure without
    ``mdk auth login`` clobbering them on the next write.

    An empty/blank starting blob gets the standard narration header.
    """
    if not text.strip():
        return _render_new_file(key, value)

    new_line = f"{key}={value}"
    out: list[str] = []
    replaced = False
    for raw in text.splitlines():
        if not replaced and _entry_key(raw) == key:
            out.append(new_line)
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        out.append(new_line)
    return "\n".join(out) + "\n"


def _delete_in_blob(text: str, key: str) -> tuple[str, bool]:
    """Return ``(new_text, removed)`` with ``key``'s line dropped if present.

    Comments + other entries are preserved — only the matching line drops.
    """
    out: list[str] = []
    removed = False
    for raw in text.splitlines():
        if not removed and _entry_key(raw) == key:
            removed = True
            continue
        out.append(raw)
    if not removed:
        return text, False
    return "\n".join(out) + "\n", True


# ---------------------------------------------------------------------------
# Backend seam
# ---------------------------------------------------------------------------


class CredentialBackend(Protocol):
    """Raw-text load/store substrate behind :class:`CredentialsStore`.

    A backend knows nothing about ``.env`` parsing — it just persists and
    returns the credentials text blob. All line editing happens above it,
    in the pure helpers, so every backend shares identical ``.env``
    semantics (comment/blank-line preservation included).
    """

    def load_text(self) -> str:
        """Return the stored credentials blob, or ``""`` if none exists."""
        ...

    def store_text(self, text: str) -> None:
        """Persist ``text`` as the credentials blob."""
        ...

    def describe(self) -> str:
        """Human-readable location for diagnostic surfaces."""
        ...


class FileCredentialBackend:
    """Default backend: plaintext ``~/.movate/credentials`` (mode 0600).

    Reads/writes the same path + ``.env`` format as before the backend
    seam existed. Writes are atomic (tempfile + rename) and the file is
    chmod'd 0600 before the swap so it's never world-readable mid-write.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _resolve_path()

    def load_text(self) -> str:
        if not self.path.is_file():
            return ""
        return self.path.read_text()

    def store_text(self, text: str) -> None:
        """Write ``text`` as one transaction (tempfile + rename), mode 0600.

        Atomic-ish: write to a sibling tempfile + rename, so an interrupted
        write never leaves a half-written credentials file.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(text)
        # Tighten permissions BEFORE the rename so the final file is
        # never world-readable during the swap.
        _chmod_owner_only(tmp_path)
        tmp_path.replace(self.path)
        _chmod_owner_only(self.path)

    def describe(self) -> str:
        return str(self.path)


class KeychainCredentialBackend:
    """Opt-in backend: the OS keychain via ``keyring``.

    The ENTIRE credentials text blob is stored as a SINGLE keyring secret
    (``keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME, blob)``).
    This is deliberate: ``keyring`` has no portable way to enumerate
    entries, so storing one blob lets :meth:`CredentialsStore.read` (which
    must return ALL pairs) work. ``get`` / ``set`` / ``delete`` all go
    read-blob → in-memory line edit → write-blob.

    ``keyring`` is imported LAZILY (only when this backend is actually
    used) so the core install with no ``keyring`` is unaffected. If the
    package is missing we raise a clear, actionable error rather than
    silently falling back to the file (that would hide operator intent).
    """

    def __init__(
        self,
        *,
        service: str = KEYCHAIN_SERVICE,
        username: str = KEYCHAIN_USERNAME,
    ) -> None:
        self.service = service
        self.username = username

    def _keyring(self) -> object:
        """Import ``keyring`` lazily, mapping ImportError → actionable error."""
        try:
            import keyring  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - exercised via simulated import in tests
            raise CredentialBackendUnavailableError(
                "the keychain credential backend needs the optional `keyring` "
                "package, which isn't installed. Install it with "
                "`pip install 'mdk[keychain]'` (or `uv pip install "
                "'movate-cli[keychain]'`), or unset MOVATE_CRED_BACKEND to use "
                "the default file backend."
            ) from exc
        return keyring

    def load_text(self) -> str:
        keyring = self._keyring()
        blob = keyring.get_password(self.service, self.username)  # type: ignore[attr-defined]
        return blob or ""

    def store_text(self, text: str) -> None:
        keyring = self._keyring()
        keyring.set_password(self.service, self.username, text)  # type: ignore[attr-defined]

    def describe(self) -> str:
        return f"OS keychain ({self.service}/{self.username})"


class CredentialBackendUnavailableError(RuntimeError):
    """Raised when a selected credential backend can't be used.

    Carries a single operator-actionable message in ``args[0]`` (e.g.
    "install `mdk[keychain]`"). CLI call sites catch this to print the
    hint and exit, rather than letting a bare ImportError surface.
    """


def _resolve_backend_name() -> str:
    """The selected backend name (``file`` | ``keychain``), default ``file``."""
    raw = os.environ.get(_BACKEND_ENV, "").strip().lower()
    return raw or "file"


def build_backend(name: str | None = None, *, path: Path | None = None) -> CredentialBackend:
    """Construct the credential backend for ``name`` (default: env-selected).

    ``file`` → :class:`FileCredentialBackend` (honors ``path`` /
    ``MOVATE_CREDENTIALS_PATH``); ``keychain`` →
    :class:`KeychainCredentialBackend`. Unknown names raise
    :class:`CredentialBackendUnavailableError` so a typo'd
    ``MOVATE_CRED_BACKEND`` fails loud instead of silently mis-routing.
    """
    resolved = (name or _resolve_backend_name()).strip().lower()
    if resolved == "file":
        return FileCredentialBackend(path=path)
    if resolved == "keychain":
        return KeychainCredentialBackend()
    raise CredentialBackendUnavailableError(
        f"unknown credential backend {resolved!r}. Set {_BACKEND_ENV} to "
        f"'file' (default) or 'keychain'."
    )


class CredentialsStore:
    """Tiny read/write helper for the machine-global credentials.

    Why a class rather than free functions: callers occasionally want
    to read + write in the same flow (e.g. ``mdk auth login`` reads
    the existing store, mutates one entry, writes it back). The class
    keeps the resolved backend stable across that pair of calls even
    if the env var changes between them.

    The default backend is the plaintext file (``~/.movate/credentials``);
    pass ``backend=`` (or set ``MOVATE_CRED_BACKEND=keychain``) to use the
    OS keychain. ``path`` is a convenience for the file backend (tests +
    multi-user systems) and is ignored when a backend is passed directly.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        backend: CredentialBackend | None = None,
    ) -> None:
        self._backend: CredentialBackend = backend or build_backend(path=path)

    @property
    def backend(self) -> CredentialBackend:
        return self._backend

    @property
    def path(self) -> Path:
        """The file path for the file backend.

        Kept for backward compatibility with the many call sites that
        print ``store.path`` ("saved to ~/.movate/credentials"). For the
        file backend this is the real path; for non-file backends it
        falls back to the canonical path purely as a display value (the
        store doesn't write there). Prefer :meth:`location` for new
        diagnostic surfaces — it describes whichever backend is active.
        """
        backend = self._backend
        if isinstance(backend, FileCredentialBackend):
            return backend.path
        return _resolve_path()

    def location(self) -> str:
        """Human-readable description of where credentials are stored."""
        return self._backend.describe()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(self) -> dict[str, str]:
        """Return every key=value pair as a dict.

        Missing store → empty dict. Comments and blank lines are
        skipped. Lines without ``=`` are silently ignored — the store
        is operator-curated, but we tolerate stray junk rather than
        breaking on malformed entries.
        """
        return _parse_blob(self._backend.load_text())

    def get(self, key: str) -> str | None:
        return self.read().get(key)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def set(self, key: str, value: str) -> None:
        """Insert or update one ``KEY=value`` entry, preserving everything else.

        A targeted line edit — existing comments, blank lines, ordering, and
        unrelated keys are left byte-for-byte intact; only the matching key's
        line is rewritten (or, for a new key, appended at the end). This is
        what lets operators keep hand-added comments/structure in the store
        without ``mdk auth login`` clobbering them on the next write.

        A fresh store gets the standard narration header. The file backend
        writes mode 0600 (Windows ignores the chmod — its home-dir ACLs
        cover it).
        """
        current = self._backend.load_text()
        self._backend.store_text(_set_in_blob(current, key, value))

    def delete(self, key: str) -> bool:
        """Remove ``key`` if present. Returns True if anything changed.

        Comments + other entries are preserved — only the matching line drops.
        """
        current = self._backend.load_text()
        new_text, removed = _delete_in_blob(current, key)
        if not removed:
            return False
        self._backend.store_text(new_text)
        return True


def _chmod_owner_only(path: Path) -> None:
    """Best-effort chmod 0600. Silently no-op on platforms that don't
    support POSIX mode bits (Windows)."""
    with contextlib.suppress(OSError, NotImplementedError):  # pragma: no cover
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _entry_key(raw: str) -> str | None:
    """Return the key of a ``KEY=value`` line, or ``None`` for comments,
    blank lines, and lines without ``=`` — so line edits skip non-entries."""
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    return line.partition("=")[0].strip()


def _render_new_file(key: str, value: str) -> str:
    """Body for a freshly-created credentials store: narration header + entry.

    Only used when the store is empty; subsequent writes preserve
    whatever the operator has (header + comments included)."""
    return (
        "\n".join(
            [
                "# movate machine-global credentials",
                "# Managed by `mdk auth login` / `mdk auth status`.",
                "# Hand-editable — same syntax as .env.",
                "# Mode 0600 — owner read/write only.",
                f"{key}={value}",
            ]
        )
        + "\n"
    )


def write_credential_to(stream: IO[str], key: str, value: str) -> None:
    """Append a single ``KEY=value`` line to ``stream``.

    Helper for callers that want to write to a stream (e.g.
    ``mdk auth login --save-to-stdout``) rather than the canonical
    file. Doesn't quote — operators copying provider API keys want
    them verbatim.
    """
    stream.write(f"{key}={value}\n")
