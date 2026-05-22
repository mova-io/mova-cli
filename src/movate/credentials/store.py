"""Read/write the user-global credentials file at ``~/.movate/credentials``.

File format is the same as ``.env`` (``KEY=value`` lines), so operators
can hand-edit it with any editor. Mode 0600 — owner read/write only.

We deliberately use ``.env`` syntax rather than YAML or TOML so the
file is grep-friendly, editor-agnostic, and copy-pasteable from
:command:`env`-style snippets common in provider docs.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path
from typing import IO

# Default location — overridable via ``MOVATE_CREDENTIALS_PATH`` for
# tests + multi-user systems. Resolves to ``~/.movate/credentials`` for
# every other invocation.
_DEFAULT_PATH = Path.home() / ".movate" / "credentials"


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


class CredentialsStore:
    """Tiny read/write helper for ``~/.movate/credentials``.

    Why a class rather than free functions: callers occasionally want
    to read + write in the same flow (e.g. ``mdk auth login`` reads
    the existing file, mutates one entry, writes it back). The class
    keeps the path resolution stable across that pair of calls even
    if the env var changes between them.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _resolve_path()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(self) -> dict[str, str]:
        """Return every key=value pair in the file as a dict.

        Missing file → empty dict. Comments and blank lines are
        skipped. Lines without ``=`` are silently ignored — the file
        is operator-curated, but we tolerate stray junk rather than
        breaking on malformed entries.
        """
        if not self.path.is_file():
            return {}
        result: dict[str, str] = {}
        for raw in self.path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip('"').strip("'")
        return result

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
        what lets operators keep hand-added comments/structure in the file
        without ``mdk auth login`` clobbering them on the next write.

        A fresh file gets the standard narration header. The file is written
        mode 0600 (Windows ignores the chmod — its home-dir ACLs cover it).
        """
        if not self.path.is_file():
            self._write_atomic_text(_render_new_file(key, value))
            return

        new_line = f"{key}={value}"
        out: list[str] = []
        replaced = False
        for raw in self.path.read_text().splitlines():
            if not replaced and _entry_key(raw) == key:
                out.append(new_line)
                replaced = True
            else:
                out.append(raw)
        if not replaced:
            out.append(new_line)
        self._write_atomic_text("\n".join(out) + "\n")

    def delete(self, key: str) -> bool:
        """Remove ``key`` if present. Returns True if anything changed.

        Comments + other entries are preserved — only the matching line drops.
        """
        if not self.path.is_file():
            return False
        out: list[str] = []
        removed = False
        for raw in self.path.read_text().splitlines():
            if not removed and _entry_key(raw) == key:
                removed = True
                continue
            out.append(raw)
        if not removed:
            return False
        self._write_atomic_text("\n".join(out) + "\n")
        return True

    def _write_atomic_text(self, body: str) -> None:
        """Write ``body`` as one transaction (tempfile + rename), mode 0600.

        Atomic-ish: write to a sibling tempfile + rename, so an interrupted
        write never leaves a half-written credentials file.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(body)
        # Tighten permissions BEFORE the rename so the final file is
        # never world-readable during the swap.
        _chmod_owner_only(tmp_path)
        tmp_path.replace(self.path)
        _chmod_owner_only(self.path)


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
    """Body for a freshly-created credentials file: narration header + entry.

    Only used when the file doesn't exist yet; subsequent writes preserve
    whatever the operator has in the file (header + comments included)."""
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
