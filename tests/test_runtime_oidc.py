"""Runtime OIDC JWT acceptance (ADR 012 D3 — server side).

The runtime can be configured (``MOVATE_OIDC_ISSUER``) to ALSO accept a
federated OIDC JWT bearer alongside the opaque ``mvt_*`` keys. These tests
exercise the full validation path through the FastAPI auth dependency
(``GET /api/v1/auth/me`` is a convenient authenticated endpoint), with the
JWKS resolution monkeypatched so **no network is used**:

* an in-test RSA keypair signs the JWTs;
* ``PyJWKClient.get_signing_key_from_jwt`` is patched to return that public
  key (and ``_fetch_discovery`` is never reached).

Coverage:
* valid signed JWT (correct aud/iss, future exp) → 200, AuthContext tenant
  taken from the configured claim;
* wrong ``aud`` → 401; wrong ``iss`` → 401; expired → 401;
* ``alg:none`` and an HS256-forged token → 401 (asymmetric-only allowlist);
* opaque ``mvt_*`` path still works while OIDC is configured;
* a JWT presented while ``MOVATE_OIDC_ISSUER`` is UNSET → 401 (no OIDC attempt).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.runtime import build_app
from movate.runtime import oidc as oidc_mod
from movate.testing import InMemoryStorage

ISSUER = "https://issuer.example.com/v2.0"
AUDIENCE = "api://movate-runtime"
TENANT_CLAIM = "tid"
KID = "test-key-1"


@pytest.fixture
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _reset_oidc_caches() -> Any:
    oidc_mod.reset_caches()
    yield
    oidc_mod.reset_caches()


@pytest.fixture
def patch_jwks(monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey) -> None:
    """Patch signing-key resolution so the public half of ``rsa_key`` is
    used to verify — no discovery + no JWKS fetch over the network."""
    public_key = rsa_key.public_key()

    class _FakeSigningKey:
        key = public_key

    def _fake_get_signing_key_from_jwt(self: Any, token: str) -> _FakeSigningKey:
        return _FakeSigningKey()

    monkeypatch.setattr(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        _fake_get_signing_key_from_jwt,
    )
    # Avoid any chance of a real discovery fetch if the client is built.
    monkeypatch.setattr(oidc_mod, "_fetch_discovery", lambda issuer: "https://jwks.example/keys")


def _configure_oidc(monkeypatch: pytest.MonkeyPatch, *, issuer: str = ISSUER) -> None:
    monkeypatch.setenv("MOVATE_OIDC_ISSUER", issuer)
    monkeypatch.setenv("MOVATE_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("MOVATE_OIDC_TENANT_CLAIM", TENANT_CLAIM)


def _make_jwt(
    rsa_key: rsa.RSAPrivateKey,
    *,
    iss: str = ISSUER,
    aud: str = AUDIENCE,
    tenant: str = "tenant-abc",
    sub: str = "user-123",
    exp_delta: int = 3600,
    extra: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        TENANT_CLAIM: tenant,
        "iat": now,
        "exp": now + exp_delta,
    }
    if extra:
        claims.update(extra)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


@pytest.mark.unit
class TestRuntimeOidcAccept:
    def test_valid_jwt_yields_auth_context(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        rsa_key: rsa.RSAPrivateKey,
        patch_jwks: None,
    ) -> None:
        _configure_oidc(monkeypatch)
        token = _make_jwt(rsa_key, tenant="tenant-from-claim", sub="alice")
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Tenant comes from the configured claim; identity is the oidc:<sub>.
        assert data["tenant_id"] == "tenant-from-claim"
        assert data["key_id"] == "oidc:alice"

    def test_default_env_when_no_env_claim(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        rsa_key: rsa.RSAPrivateKey,
        patch_jwks: None,
    ) -> None:
        _configure_oidc(monkeypatch)
        monkeypatch.setenv("MOVATE_OIDC_DEFAULT_ENV", "test")
        token = _make_jwt(rsa_key)
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["env"] == "test"

    def test_wrong_audience_401(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        rsa_key: rsa.RSAPrivateKey,
        patch_jwks: None,
    ) -> None:
        _configure_oidc(monkeypatch)
        token = _make_jwt(rsa_key, aud="api://someone-else")
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_no_audience_configured_401(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        rsa_key: rsa.RSAPrivateKey,
        patch_jwks: None,
    ) -> None:
        # OIDC enabled (issuer set) but MOVATE_OIDC_AUDIENCE unset → fail closed:
        # an otherwise-valid token is rejected rather than accepted with an
        # unchecked audience (shared-issuer privilege-escalation guard).
        monkeypatch.setenv("MOVATE_OIDC_ISSUER", ISSUER)
        monkeypatch.setenv("MOVATE_OIDC_TENANT_CLAIM", TENANT_CLAIM)
        monkeypatch.delenv("MOVATE_OIDC_AUDIENCE", raising=False)
        token = _make_jwt(rsa_key)
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_wrong_issuer_401(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        rsa_key: rsa.RSAPrivateKey,
        patch_jwks: None,
    ) -> None:
        _configure_oidc(monkeypatch)
        token = _make_jwt(rsa_key, iss="https://evil.example.com/v2.0")
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_expired_401(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        rsa_key: rsa.RSAPrivateKey,
        patch_jwks: None,
    ) -> None:
        _configure_oidc(monkeypatch)
        # Well past the 60s leeway.
        token = _make_jwt(rsa_key, exp_delta=-3600)
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_missing_tenant_claim_401(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        rsa_key: rsa.RSAPrivateKey,
        patch_jwks: None,
    ) -> None:
        _configure_oidc(monkeypatch)
        # Point the tenant claim at one the token doesn't carry.
        monkeypatch.setenv("MOVATE_OIDC_TENANT_CLAIM", "nonexistent_claim")
        token = _make_jwt(rsa_key)
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_alg_none_forged_token_401(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        rsa_key: rsa.RSAPrivateKey,
        patch_jwks: None,
    ) -> None:
        _configure_oidc(monkeypatch)
        now = int(time.time())
        # Unsigned token (alg:none) — must be rejected by the asym-only allowlist.
        forged = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": "attacker",
                TENANT_CLAIM: "tenant-evil",
                "exp": now + 3600,
            },
            key="",
            algorithm="none",
        )
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
        assert resp.status_code == 401

    def test_hs256_forged_token_401(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        rsa_key: rsa.RSAPrivateKey,
        patch_jwks: None,
    ) -> None:
        _configure_oidc(monkeypatch)
        now = int(time.time())
        # Classic downgrade attack: forge an HS256 token using the server's
        # PUBLIC RSA key bytes as the HMAC secret. PyJWT's own `encode`
        # refuses to HMAC a PEM key, so we hand-assemble the compact JWS the
        # way an attacker would. The asymmetric-only allowlist must refuse to
        # even consider HS256 → 401, irrespective of the signature.
        pub_pem = rsa_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        def _b64(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
        payload = _b64(
            json.dumps(
                {
                    "iss": ISSUER,
                    "aud": AUDIENCE,
                    "sub": "attacker",
                    TENANT_CLAIM: "tenant-evil",
                    "exp": now + 3600,
                }
            ).encode()
        )
        signing_input = f"{header}.{payload}".encode()
        sig = hmac.new(pub_pem, signing_input, hashlib.sha256).digest()
        forged = f"{header}.{payload}.{_b64(sig)}"
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
        assert resp.status_code == 401


@pytest.mark.unit
class TestOpaquePathStillWorks:
    async def test_opaque_key_works_while_oidc_configured(
        self,
        client: TestClient,
        storage: InMemoryStorage,
        monkeypatch: pytest.MonkeyPatch,
        patch_jwks: None,
    ) -> None:
        # OIDC is ON, but an opaque mvt_* key must still authenticate exactly
        # as before — the token-shape branch routes it to the opaque path.
        _configure_oidc(monkeypatch)
        minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="opaque")
        await storage.save_api_key(minted.record)
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["key_id"] == minted.record.key_id

    def test_jwt_rejected_when_oidc_unset(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        rsa_key: rsa.RSAPrivateKey,
        patch_jwks: None,
    ) -> None:
        # Master switch OFF: a JWT is NOT treated as OIDC; it falls through to
        # the opaque parser (which rejects it) — today's byte-for-byte 401.
        monkeypatch.delenv("MOVATE_OIDC_ISSUER", raising=False)
        token = _make_jwt(rsa_key)
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
