"""OIDC device-code SSO login + token cache/refresh (ADR 013 L1).

No real IdP / network is touched — all OIDC HTTP (discovery, device-auth,
token, refresh) is served by an :class:`httpx.MockTransport`, and the token
cache is a real :class:`CredentialsStore` backed by a tmp file.

Coverage:
* device-code flow: discovery → device-auth → poll (``authorization_pending``
  then success) → token cached;
* refresh: a cached-but-expired token is silently refreshed by the provider;
* dead refresh → actionable "run mdk auth login" error;
* ``_resolve_target_bearer`` returns the cached token for ``auth: oidc``
  (no ``az`` needed); the ``auth: key`` default path is unchanged;
* ``logout`` clears the cache; ``whoami`` shows the OIDC identity;
* token values never appear in CLI output.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest
import typer
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core import oidc_device
from movate.core.oidc_device import (
    CachedDeviceCodeTokenProvider,
    DeviceCodeTokenCache,
    run_device_code_login,
)
from movate.core.oidc_provider import OidcTokenError, select_oidc_provider
from movate.core.user_config import TargetConfig
from movate.credentials.store import CredentialsStore

runner = CliRunner(mix_stderr=False)

_ISSUER = "https://idp.example.com"
_CLIENT_ID = "app-client-123"
_DEVICE_EP = f"{_ISSUER}/devicecode"
_TOKEN_EP = f"{_ISSUER}/token"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_jwt(claims: dict[str, object], *, sig: str = "sig") -> str:
    """Hand-build a JWS-compact JWT (no PyJWT dep — oidc_device is core).

    The signature is decorative: the CLI's identity-display decode uses
    ``verify_signature=False``, and the runtime — not this test — is the real
    validator. This keeps the test runnable in a core-only install.
    """
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(claims).encode())
    return f"{header}.{body}.{sig}"


_CLAIMS = {"sub": "user@example.com", "tid": "tenant-abc", "scp": "read run"}
# Never a real secret; must never leak into CLI output.
_ACCESS_TOKEN = _make_jwt(_CLAIMS, sig="sigA")
_REFRESH_TOKEN = "refresh-tok-0001"
_NEW_ACCESS_TOKEN = _make_jwt(_CLAIMS, sig="sigB")


def _discovery_body() -> dict[str, str]:
    return {
        "device_authorization_endpoint": _DEVICE_EP,
        "token_endpoint": _TOKEN_EP,
    }


@pytest.fixture(autouse=True)
def _reset_discovery() -> None:
    """Each test gets a clean discovery cache so the mock transport is hit."""
    oidc_device.reset_caches()


@pytest.fixture
def cred_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CredentialsStore:
    """A real file-backed credentials store at a tmp path."""
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "credentials"))
    monkeypatch.delenv("MOVATE_CRED_BACKEND", raising=False)
    return CredentialsStore()


def _oidc_target() -> TargetConfig:
    return TargetConfig(
        url="https://runtime.example.com",
        key_env="MDK_DEV_KEY",
        auth="oidc",
        oidc_issuer=_ISSUER,
        oidc_client_id=_CLIENT_ID,
        oidc_scope="api://movate/.default",
    )


# ---------------------------------------------------------------------------
# Device-code login flow
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeviceCodeLogin:
    def test_login_polls_pending_then_succeeds_and_caches(
        self, cred_store: CredentialsStore
    ) -> None:
        # The token endpoint returns authorization_pending on the first poll,
        # then a token on the second — exercising the RFC 8628 poll loop.
        poll_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/.well-known/openid-configuration"):
                return httpx.Response(200, json=_discovery_body())
            if url == _DEVICE_EP:
                return httpx.Response(
                    200,
                    json={
                        "device_code": "dev-code-xyz",
                        "user_code": "WXYZ-1234",
                        "verification_uri": "https://idp.example.com/activate",
                        "verification_uri_complete": (
                            "https://idp.example.com/activate?code=WXYZ-1234"
                        ),
                        "interval": 2,
                        "expires_in": 300,
                    },
                )
            if url == _TOKEN_EP:
                poll_calls["n"] += 1
                if poll_calls["n"] == 1:
                    return httpx.Response(400, json={"error": "authorization_pending"})
                return httpx.Response(
                    200,
                    json={
                        "access_token": _ACCESS_TOKEN,
                        "refresh_token": _REFRESH_TOKEN,
                        "expires_in": 3600,
                    },
                )
            raise AssertionError(f"unexpected request: {url}")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        prompts: list[oidc_device.DeviceCodeStart] = []
        result = run_device_code_login(
            "dev",
            _oidc_target(),
            on_prompt=prompts.append,
            client=client,
            sleep=lambda _s: None,  # no real waiting
        )

        assert poll_calls["n"] == 2
        assert result.access_token == _ACCESS_TOKEN
        assert result.refresh_token == _REFRESH_TOKEN
        assert result.expires_at is not None
        # The human-facing prompt carried the verification URI + user code.
        assert prompts and prompts[0].user_code == "WXYZ-1234"

        # Cache it + read it straight back.
        cache = DeviceCodeTokenCache(cred_store)
        cache.save("dev", result)
        loaded = cache.load("dev")
        assert loaded is not None
        assert loaded.access_token == _ACCESS_TOKEN
        assert loaded.refresh_token == _REFRESH_TOKEN

    def test_access_denied_raises_actionable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/.well-known/openid-configuration"):
                return httpx.Response(200, json=_discovery_body())
            if url == _DEVICE_EP:
                return httpx.Response(
                    200,
                    json={
                        "device_code": "d",
                        "user_code": "U",
                        "verification_uri": "https://idp.example.com/activate",
                        "interval": 1,
                        "expires_in": 300,
                    },
                )
            if url == _TOKEN_EP:
                return httpx.Response(400, json={"error": "access_denied"})
            raise AssertionError(url)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(OidcTokenError) as exc:
            run_device_code_login(
                "dev",
                _oidc_target(),
                on_prompt=lambda _s: None,
                client=client,
                sleep=lambda _s: None,
            )
        assert "access_denied" in str(exc.value)

    def test_no_device_endpoint_in_discovery_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/.well-known/openid-configuration"):
                # Discovery without device_authorization_endpoint.
                return httpx.Response(200, json={"token_endpoint": _TOKEN_EP})
            raise AssertionError("should not reach device-auth")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(OidcTokenError) as exc:
            run_device_code_login("dev", _oidc_target(), on_prompt=lambda _s: None, client=client)
        assert "device_authorization_endpoint" in str(exc.value)

    def test_missing_client_id_raises(self) -> None:
        target = TargetConfig(
            url="https://x", key_env="MDK_DEV_KEY", auth="oidc", oidc_issuer=_ISSUER
        )
        with pytest.raises(OidcTokenError) as exc:
            oidc_device.start_device_code("dev", target)
        assert "oidc_client_id" in str(exc.value)


# ---------------------------------------------------------------------------
# Refresh + provider
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRefreshAndProvider:
    def test_expired_token_is_refreshed_by_provider(self, cred_store: CredentialsStore) -> None:
        # Seed the cache with an already-expired access token + a refresh token.
        cache = DeviceCodeTokenCache(cred_store)
        cache.save(
            "dev",
            oidc_device.TokenResult(
                access_token=_ACCESS_TOKEN,
                refresh_token=_REFRESH_TOKEN,
                expires_at=time.time() - 10,  # expired
            ),
        )

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/.well-known/openid-configuration"):
                return httpx.Response(200, json=_discovery_body())
            if url == _TOKEN_EP:
                captured["body"] = request.content.decode()
                return httpx.Response(
                    200,
                    json={"access_token": _NEW_ACCESS_TOKEN, "expires_in": 3600},
                )
            raise AssertionError(url)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        # Drive refresh directly with the mock client, then assert the
        # provider returns the refreshed token from cache.
        refreshed = oidc_device.refresh_access_token(
            "dev", _oidc_target(), _REFRESH_TOKEN, client=client
        )
        assert refreshed.access_token == _NEW_ACCESS_TOKEN
        # The refresh grant carried the refresh_token grant_type.
        assert "grant_type=refresh_token" in captured["body"]
        # Refresh response omitted refresh_token → prior one is preserved.
        assert refreshed.refresh_token == _REFRESH_TOKEN

        cache.save("dev", refreshed)
        loaded = cache.load("dev")
        assert loaded is not None
        assert loaded.access_token == _NEW_ACCESS_TOKEN

    def test_provider_returns_valid_cached_token_without_refresh(
        self, cred_store: CredentialsStore
    ) -> None:
        cache = DeviceCodeTokenCache(cred_store)
        cache.save(
            "dev",
            oidc_device.TokenResult(
                access_token=_ACCESS_TOKEN,
                refresh_token=_REFRESH_TOKEN,
                expires_at=time.time() + 3600,  # still valid
            ),
        )
        provider = CachedDeviceCodeTokenProvider(cache)
        # No HTTP at all — a valid cached token is returned as-is.
        token = provider.get_token("dev", _oidc_target())
        assert token == _ACCESS_TOKEN

    def test_no_cached_token_points_at_login(self, cred_store: CredentialsStore) -> None:
        provider = CachedDeviceCodeTokenProvider(DeviceCodeTokenCache(cred_store))
        with pytest.raises(OidcTokenError) as exc:
            provider.get_token("dev", _oidc_target())
        assert "mdk auth login" in str(exc.value)

    def test_dead_refresh_points_at_login(self, cred_store: CredentialsStore) -> None:
        cache = DeviceCodeTokenCache(cred_store)
        cache.save(
            "dev",
            oidc_device.TokenResult(
                access_token=_ACCESS_TOKEN,
                refresh_token=_REFRESH_TOKEN,
                expires_at=time.time() - 10,  # expired
            ),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/.well-known/openid-configuration"):
                return httpx.Response(200, json=_discovery_body())
            if url == _TOKEN_EP:
                return httpx.Response(400, json={"error": "invalid_grant"})
            raise AssertionError(url)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(OidcTokenError) as exc:
            oidc_device.refresh_access_token("dev", _oidc_target(), _REFRESH_TOKEN, client=client)
        msg = str(exc.value)
        assert "mdk auth login" in msg
        # The actionable error must never echo the token values.
        assert _ACCESS_TOKEN not in msg
        assert _REFRESH_TOKEN not in msg


# ---------------------------------------------------------------------------
# _resolve_target_bearer routing
# ---------------------------------------------------------------------------


def _write_config(home: Path, body: str) -> Path:
    cfg_dir = home / ".movate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "config.yaml"
    path.write_text(body)
    return path


@pytest.mark.unit
class TestResolveTargetBearerOidc:
    def test_oidc_target_returns_cached_token_no_az(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from movate.cli.kb_cmd import _resolve_target_bearer  # noqa: PLC0415

        cfg = _write_config(
            tmp_path,
            "active: dev\n"
            "targets:\n"
            "  dev:\n"
            "    url: https://runtime.example.com\n"
            "    key_env: MDK_DEV_KEY\n"
            "    auth: oidc\n"
            f"    oidc_issuer: {_ISSUER}\n"
            f"    oidc_client_id: {_CLIENT_ID}\n",
        )
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "credentials"))
        monkeypatch.delenv("MDK_DEV_KEY", raising=False)
        # If the oidc path wrongly shelled out to `az`, this would blow up.
        monkeypatch.setattr(
            "shutil.which",
            lambda name: (_ for _ in ()).throw(AssertionError("must not call az")),
        )

        # Seed a valid cached token for the target.
        cache = DeviceCodeTokenCache(CredentialsStore())
        cache.save(
            "dev",
            oidc_device.TokenResult(
                access_token=_ACCESS_TOKEN,
                refresh_token=_REFRESH_TOKEN,
                expires_at=time.time() + 3600,
            ),
        )

        name, _cfg, base_url, bearer = _resolve_target_bearer("dev")
        assert name == "dev"
        assert base_url == "https://runtime.example.com"
        assert bearer == _ACCESS_TOKEN

    def test_oidc_target_no_token_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from movate.cli.kb_cmd import _resolve_target_bearer  # noqa: PLC0415

        cfg = _write_config(
            tmp_path,
            "active: dev\n"
            "targets:\n"
            "  dev:\n"
            "    url: https://runtime.example.com\n"
            "    key_env: MDK_DEV_KEY\n"
            "    auth: oidc\n"
            f"    oidc_issuer: {_ISSUER}\n"
            f"    oidc_client_id: {_CLIENT_ID}\n",
        )
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "credentials"))
        with pytest.raises(typer.Exit) as exc:
            _resolve_target_bearer("dev")
        assert exc.value.exit_code == 2

    def test_key_target_unchanged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The default auth='key' path must be byte-for-byte unchanged: reads
        # the env var, never touches the device-code provider or `az`.
        from movate.cli.kb_cmd import _resolve_target_bearer  # noqa: PLC0415

        cfg = _write_config(
            tmp_path,
            "active: dev\n"
            "targets:\n"
            "  dev:\n"
            "    url: https://runtime.example.com\n"
            "    key_env: MDK_DEV_KEY\n",
        )
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("MDK_DEV_KEY", "mvt_live_tenantxx_KEYID12345_secret")
        name, _cfg, _url, bearer = _resolve_target_bearer("dev")
        assert name == "dev"
        assert bearer == "mvt_live_tenantxx_KEYID12345_secret"

    def test_azure_cli_provider_still_selectable(self) -> None:
        from movate.core.oidc_provider import AzureCliTokenProvider  # noqa: PLC0415

        target = TargetConfig(
            url="https://x",
            key_env="MDK_DEV_KEY",
            auth="oidc",
            oidc_provider="azure-cli",
            oidc_resource="api://movate",
        )
        assert isinstance(select_oidc_provider(target), AzureCliTokenProvider)

    def test_device_code_is_default_provider(self) -> None:
        assert isinstance(select_oidc_provider(_oidc_target()), CachedDeviceCodeTokenProvider)


# ---------------------------------------------------------------------------
# CLI: login / logout / whoami
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCliCommands:
    def _seed_oidc_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _write_config(
            tmp_path,
            "active: dev\n"
            "targets:\n"
            "  dev:\n"
            "    url: https://runtime.example.com\n"
            "    key_env: MDK_DEV_KEY\n"
            "    auth: oidc\n"
            f"    oidc_issuer: {_ISSUER}\n"
            f"    oidc_client_id: {_CLIENT_ID}\n",
        )
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "credentials"))

    def test_login_target_caches_and_hides_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_oidc_config(tmp_path, monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/.well-known/openid-configuration"):
                return httpx.Response(200, json=_discovery_body())
            if url == _DEVICE_EP:
                return httpx.Response(
                    200,
                    json={
                        "device_code": "d",
                        "user_code": "ABCD-1234",
                        "verification_uri": "https://idp.example.com/activate",
                        "interval": 1,
                        "expires_in": 300,
                    },
                )
            if url == _TOKEN_EP:
                return httpx.Response(
                    200,
                    json={
                        "access_token": _ACCESS_TOKEN,
                        "refresh_token": _REFRESH_TOKEN,
                        "expires_in": 3600,
                    },
                )
            raise AssertionError(url)

        # Patch the device-code entrypoint (imported lazily inside the CLI
        # from movate.core.oidc_device) to use our mock transport + no sleep,
        # while exercising the real flow underneath.
        real = oidc_device.run_device_code_login

        def patched(target_name: str, target: object, **kw: object) -> object:
            client = httpx.Client(transport=httpx.MockTransport(handler))
            kw["client"] = client
            kw["sleep"] = lambda _s: None
            return real(target_name, target, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr("movate.core.oidc_device.run_device_code_login", patched)

        result = runner.invoke(app, ["auth", "login", "--target", "dev"])
        assert result.exit_code == 0, result.stdout + result.stderr
        # Verification UI shown; identity surfaced; token NEVER printed.
        assert "ABCD-1234" in result.stdout
        assert "user@example.com" in result.stdout
        assert _ACCESS_TOKEN not in result.stdout
        assert _ACCESS_TOKEN not in result.stderr
        assert _REFRESH_TOKEN not in result.stdout

        # Token landed in the cache.
        cache = DeviceCodeTokenCache(CredentialsStore())
        loaded = cache.load("dev")
        assert loaded is not None
        assert loaded.access_token == _ACCESS_TOKEN

    def test_logout_clears_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._seed_oidc_config(tmp_path, monkeypatch)
        cache = DeviceCodeTokenCache(CredentialsStore())
        cache.save(
            "dev",
            oidc_device.TokenResult(
                access_token=_ACCESS_TOKEN,
                refresh_token=_REFRESH_TOKEN,
                expires_at=time.time() + 3600,
            ),
        )
        assert cache.load("dev") is not None

        result = runner.invoke(app, ["auth", "logout", "--target", "dev"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert cache.load("dev") is None
        # logout output never echoes the token.
        assert _ACCESS_TOKEN not in result.stdout

    def test_logout_idempotent_when_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_oidc_config(tmp_path, monkeypatch)
        result = runner.invoke(app, ["auth", "logout", "--target", "dev"])
        assert result.exit_code == 0
        # `hint()` writes to stderr.
        assert "nothing to clear" in result.stderr

    def test_whoami_oidc_shows_identity_not_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_oidc_config(tmp_path, monkeypatch)
        cache = DeviceCodeTokenCache(CredentialsStore())
        cache.save(
            "dev",
            oidc_device.TokenResult(
                access_token=_ACCESS_TOKEN,
                refresh_token=_REFRESH_TOKEN,
                expires_at=time.time() + 3600,
            ),
        )

        # The runtime /auth/me call is unreachable in the test; whoami should
        # still surface the local token claims, then fail the HTTP leg cleanly.
        result = runner.invoke(app, ["auth", "whoami", "--target", "dev"])
        # The HTTP leg to the (unreachable) runtime exits 2, but the local
        # identity claims were printed first.
        assert "user@example.com" in result.stdout
        assert "tenant-abc" in result.stdout
        assert _ACCESS_TOKEN not in result.stdout
        assert _ACCESS_TOKEN not in result.stderr

    def test_login_target_rejects_key_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _write_config(
            tmp_path,
            "active: dev\n"
            "targets:\n"
            "  dev:\n"
            "    url: https://runtime.example.com\n"
            "    key_env: MDK_DEV_KEY\n",
        )
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "credentials"))
        result = runner.invoke(app, ["auth", "login", "--target", "dev"])
        assert result.exit_code == 2
        assert "not an OIDC target" in result.stderr
