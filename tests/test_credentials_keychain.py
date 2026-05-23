"""ADR 012 D2 — optional OS-keychain credential backend.

Covers the keychain backend behind :class:`CredentialsStore`:

1. **Keychain round-trip** — set/get/read/delete against a fake
   in-memory keyring (no real OS keychain access).
2. **File backend stays the default** + comment/blank-line preservation
   still holds (the existing behavior must not regress).
3. **`MOVATE_CRED_BACKEND=keychain` with `keyring` absent** → a clear,
   actionable error (the missing import is simulated).
4. **`mdk auth use-keychain` / `use-file`** migration — copies across
   backends, leaves the source intact unless ``--remove-source``.
5. **`autoload_credentials` is backend-agnostic** — fills env from
   whichever backend is selected.

No real OS keychain is ever touched: the keychain backend is pointed at
an in-memory fake via ``keyring.set_keyring(...)``. The whole module
skips cleanly if ``keyring`` can't be imported in the test env.
"""

from __future__ import annotations

import builtins
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

keyring = pytest.importorskip("keyring", reason="keyring not installed in this env")
from keyring.backend import KeyringBackend  # noqa: E402

from movate.cli.main import app  # noqa: E402
from movate.credentials import (  # noqa: E402
    CredentialBackendUnavailableError,
    CredentialsStore,
    KeychainCredentialBackend,
    autoload_credentials,
    build_backend,
)
from movate.credentials.loader import PROVIDER_KEY_ENV_VARS  # noqa: E402
from movate.credentials.store import KEYCHAIN_SERVICE, KEYCHAIN_USERNAME  # noqa: E402

runner = CliRunner(mix_stderr=False)


class _FakeKeyring(KeyringBackend):
    """In-memory keyring backend for tests — never touches the real OS.

    Implements just the three operations the keychain credential backend
    uses (``set``/``get``/``delete``). Installed via
    ``keyring.set_keyring(...)`` so ``keyring.get_password`` and friends
    route here instead of the platform keychain.
    """

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeKeyring]:
    """Install an in-memory keyring + select the keychain backend.

    Restores the operator's real keyring on teardown so no test bleeds
    into the next (or into the developer's actual OS keychain)."""
    original = keyring.get_keyring()
    fake = _FakeKeyring()
    keyring.set_keyring(fake)
    monkeypatch.setenv("MOVATE_CRED_BACKEND", "keychain")
    # Strip provider env vars so autoload tests start clean.
    for key in PROVIDER_KEY_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    try:
        yield fake
    finally:
        keyring.set_keyring(original)


@pytest.fixture
def isolated_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the file backend at a tempfile + force the file backend."""
    path = tmp_path / "credentials"
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(path))
    monkeypatch.setenv("MOVATE_CRED_BACKEND", "file")
    for key in PROVIDER_KEY_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    return path


# ---------------------------------------------------------------------------
# 1. Keychain backend round-trip (fake in-memory keyring)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKeychainRoundTrip:
    def test_set_get_read_delete(self, fake_keyring: _FakeKeyring) -> None:
        store = CredentialsStore()
        # Sanity: env-selection actually routed to the keychain backend.
        assert isinstance(store.backend, KeychainCredentialBackend)

        store.set("OPENAI_API_KEY", "sk-kc-1")
        store.set("ANTHROPIC_API_KEY", "ant-kc-2")

        assert store.get("OPENAI_API_KEY") == "sk-kc-1"
        assert store.get("ANTHROPIC_API_KEY") == "ant-kc-2"
        assert store.read() == {
            "OPENAI_API_KEY": "sk-kc-1",
            "ANTHROPIC_API_KEY": "ant-kc-2",
        }

        assert store.delete("OPENAI_API_KEY") is True
        assert store.get("OPENAI_API_KEY") is None
        assert store.read() == {"ANTHROPIC_API_KEY": "ant-kc-2"}

    def test_stored_as_single_blob(self, fake_keyring: _FakeKeyring) -> None:
        """The whole credentials text is ONE keyring secret (the
        no-enumeration workaround), not one entry per key."""
        store = CredentialsStore()
        store.set("OPENAI_API_KEY", "sk-1")
        store.set("ANTHROPIC_API_KEY", "ant-1")
        # Exactly one keyring entry under the movate service/username.
        assert list(fake_keyring.store.keys()) == [(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME)]
        blob = fake_keyring.store[(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME)]
        assert "OPENAI_API_KEY=sk-1" in blob
        assert "ANTHROPIC_API_KEY=ant-1" in blob

    def test_update_existing_key(self, fake_keyring: _FakeKeyring) -> None:
        store = CredentialsStore()
        store.set("OPENAI_API_KEY", "sk-old")
        store.set("OPENAI_API_KEY", "sk-new")
        assert store.get("OPENAI_API_KEY") == "sk-new"
        assert store.read() == {"OPENAI_API_KEY": "sk-new"}

    def test_delete_missing_returns_false(self, fake_keyring: _FakeKeyring) -> None:
        assert CredentialsStore().delete("NOPE") is False


# ---------------------------------------------------------------------------
# 2. File backend remains the default + preserves comments/blank lines
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFileBackendDefault:
    def test_file_is_default_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No MOVATE_CRED_BACKEND → the file backend, writing to the
        configured path."""
        path = tmp_path / "credentials"
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(path))
        monkeypatch.delenv("MOVATE_CRED_BACKEND", raising=False)
        from movate.credentials import FileCredentialBackend  # noqa: PLC0415

        store = CredentialsStore()
        assert isinstance(store.backend, FileCredentialBackend)
        store.set("OPENAI_API_KEY", "sk-file")
        assert path.is_file()
        assert store.get("OPENAI_API_KEY") == "sk-file"

    def test_comment_and_blank_line_preservation(self, isolated_file: Path) -> None:
        """The byte-for-byte comment-preservation guarantee survives the
        backend refactor (mirrors the existing store test)."""
        isolated_file.parent.mkdir(parents=True, exist_ok=True)
        isolated_file.write_text(
            "# my notes\nOPENAI_API_KEY=sk-old\n\n# section two\nANTHROPIC_API_KEY=ant-1\n"
        )
        CredentialsStore().set("OPENAI_API_KEY", "sk-new")
        text = isolated_file.read_text()
        assert "# my notes" in text
        assert "# section two" in text
        assert "OPENAI_API_KEY=sk-new" in text
        assert "sk-old" not in text
        assert "ANTHROPIC_API_KEY=ant-1" in text
        # Blank line between the two sections survives.
        assert "\n\n# section two" in text
        # Ordering preserved.
        assert text.index("OPENAI_API_KEY") < text.index("# section two")

    def test_file_mode_0600(self, isolated_file: Path) -> None:
        CredentialsStore().set("OPENAI_API_KEY", "sk-x")
        mode = isolated_file.stat().st_mode & 0o777
        # Windows can't set POSIX bits — skip the assert there.
        if os.name == "posix":
            assert mode == 0o600, f"expected 0600, got {oct(mode)}"


# ---------------------------------------------------------------------------
# 3. keychain selected but `keyring` absent → actionable error
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKeyringAbsent:
    def test_missing_keyring_raises_actionable_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simulate `keyring` not installed: the backend must raise a
        clear error pointing at `mdk[keychain]`, NOT silently fall back."""
        import sys  # noqa: PLC0415

        monkeypatch.setenv("MOVATE_CRED_BACKEND", "keychain")

        # Drop any cached `keyring` modules so the lazy `import keyring`
        # actually re-runs the import machinery (and hits our shim).
        for mod in list(sys.modules):
            if mod == "keyring" or mod.startswith("keyring."):
                monkeypatch.delitem(sys.modules, mod, raising=False)

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "keyring" or name.startswith("keyring."):
                raise ImportError("No module named 'keyring'")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)

        # Building the store/backend is fine (lazy import); the error
        # surfaces when an operation actually touches keyring.
        store = CredentialsStore()
        with pytest.raises(CredentialBackendUnavailableError) as exc:
            store.read()
        msg = str(exc.value).lower()
        assert "keyring" in msg
        assert "keychain" in msg  # points at the `mdk[keychain]` extra

    def test_unknown_backend_name_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MOVATE_CRED_BACKEND", "bogus")
        with pytest.raises(CredentialBackendUnavailableError) as exc:
            build_backend()
        assert "bogus" in str(exc.value)


# ---------------------------------------------------------------------------
# 4. Migration commands: `mdk auth use-keychain` / `use-file`
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMigrationCommand:
    def test_use_keychain_copies_and_keeps_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "credentials"
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(path))
        for key in PROVIDER_KEY_ENV_VARS:
            monkeypatch.delenv(key, raising=False)
        # Seed the FILE backend with two keys.
        file_store = CredentialsStore(backend=build_backend("file"))
        file_store.set("OPENAI_API_KEY", "sk-file-1")
        file_store.set("ANTHROPIC_API_KEY", "ant-file-2")

        original = keyring.get_keyring()
        fake = _FakeKeyring()
        keyring.set_keyring(fake)
        try:
            # The CLI defaults to the file backend unless overridden;
            # the command itself targets file→keychain explicitly.
            monkeypatch.delenv("MOVATE_CRED_BACKEND", raising=False)
            result = runner.invoke(app, ["auth", "use-keychain"], env={"COLUMNS": "200"})
            assert result.exit_code == 0, result.stdout + result.stderr

            kc_store = CredentialsStore(backend=build_backend("keychain"))
            assert kc_store.read() == {
                "OPENAI_API_KEY": "sk-file-1",
                "ANTHROPIC_API_KEY": "ant-file-2",
            }
            # Source file untouched (no --remove-source).
            assert file_store.read() == {
                "OPENAI_API_KEY": "sk-file-1",
                "ANTHROPIC_API_KEY": "ant-file-2",
            }
            # Never log secret VALUES — only key names.
            combined = result.stdout + result.stderr
            assert "OPENAI_API_KEY" in combined
            assert "sk-file-1" not in combined
        finally:
            keyring.set_keyring(original)

    def test_use_keychain_remove_source_clears_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "credentials"
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(path))
        for key in PROVIDER_KEY_ENV_VARS:
            monkeypatch.delenv(key, raising=False)
        file_store = CredentialsStore(backend=build_backend("file"))
        file_store.set("OPENAI_API_KEY", "sk-1")

        original = keyring.get_keyring()
        fake = _FakeKeyring()
        keyring.set_keyring(fake)
        try:
            monkeypatch.delenv("MOVATE_CRED_BACKEND", raising=False)
            result = runner.invoke(
                app, ["auth", "use-keychain", "--remove-source"], env={"COLUMNS": "200"}
            )
            assert result.exit_code == 0, result.stdout + result.stderr
            # Copied into keychain...
            kc_store = CredentialsStore(backend=build_backend("keychain"))
            assert kc_store.get("OPENAI_API_KEY") == "sk-1"
            # ...AND removed from the file.
            assert file_store.get("OPENAI_API_KEY") is None
        finally:
            keyring.set_keyring(original)

    def test_use_file_reverse_direction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "credentials"
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(path))
        for key in PROVIDER_KEY_ENV_VARS:
            monkeypatch.delenv(key, raising=False)

        original = keyring.get_keyring()
        fake = _FakeKeyring()
        keyring.set_keyring(fake)
        try:
            # Seed the KEYCHAIN backend.
            kc_store = CredentialsStore(backend=build_backend("keychain"))
            kc_store.set("OPENAI_API_KEY", "sk-kc")

            monkeypatch.delenv("MOVATE_CRED_BACKEND", raising=False)
            result = runner.invoke(app, ["auth", "use-file"], env={"COLUMNS": "200"})
            assert result.exit_code == 0, result.stdout + result.stderr

            file_store = CredentialsStore(backend=build_backend("file"))
            assert file_store.get("OPENAI_API_KEY") == "sk-kc"
            # Keychain source intact (no --remove-source).
            assert kc_store.get("OPENAI_API_KEY") == "sk-kc"
        finally:
            keyring.set_keyring(original)

    def test_use_keychain_empty_source_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "credentials"
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(path))
        for key in PROVIDER_KEY_ENV_VARS:
            monkeypatch.delenv(key, raising=False)
        original = keyring.get_keyring()
        keyring.set_keyring(_FakeKeyring())
        try:
            monkeypatch.delenv("MOVATE_CRED_BACKEND", raising=False)
            result = runner.invoke(app, ["auth", "use-keychain"], env={"COLUMNS": "200"})
            assert result.exit_code == 0, result.stdout + result.stderr
            assert "nothing to migrate" in (result.stdout + result.stderr).lower()
        finally:
            keyring.set_keyring(original)


# ---------------------------------------------------------------------------
# 5. autoload_credentials is backend-agnostic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoloadBackendAgnostic:
    def test_autoload_reads_through_keychain(self, fake_keyring: _FakeKeyring) -> None:
        """With the keychain backend selected, autoload fills env vars
        from the keychain just as it would from the file."""
        CredentialsStore().set("OPENAI_API_KEY", "sk-from-keychain")
        assert os.environ.get("OPENAI_API_KEY") is None
        autoload_credentials()
        assert os.environ["OPENAI_API_KEY"] == "sk-from-keychain"

    def test_autoload_runtime_key_through_keychain(
        self, fake_keyring: _FakeKeyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MDK_<T>_KEY pattern autoload also works through the keychain
        backend (the runtime-bearer flow stays untouched)."""
        # Clear any real shell-set runtime key so autoload (which never
        # clobbers a set value) actually fills from the keychain.
        monkeypatch.delenv("MDK_DEV_KEY", raising=False)
        CredentialsStore().set("MDK_DEV_KEY", "mvt_live_x")
        autoload_credentials()
        assert os.environ["MDK_DEV_KEY"] == "mvt_live_x"
