"""Unit tests for ``movate.integrations.github``.

Every test uses ``httpx.MockTransport`` so we never touch the real
GitHub API. We also generate a fresh RSA private key per test run via
``cryptography`` (no fixtures committed in PEM form) — keeps the test
suite hermetic + lets us assert against the JWT we sign.

Coverage:

* ``is_enabled`` — every truthy/falsey env shape
* ``GitHubConfig.from_env`` — happy path, missing var → 422-tagged
  ``GitHubError``, malformed int → 422
* ``GitHubClient`` JWT signing — header/payload/signature shape +
  cache hit (no second JWT for back-to-back calls)
* Installation-token exchange — 201 happy path, 401 fail → 502
  ``GitHubError`` with the upstream status preserved
* ``publish_bundle`` — five-step Git Data API dance lands the right
  body shapes on each endpoint, returns the right ``PublishResult``
* ``publish_bundle`` — empty bundle dir → 422; missing dir → 404
* ``publish_bundle`` — dot-prefixed files are excluded from the
  committed tree (matches the registry's scan rules)

We don't smoke-test the live GitHub API — that would require a real
App + repo + KV-stored private key, which is operator-side setup. The
contract test is "we send the body shape the docs prescribe"; the
``MockTransport`` captures every request so the assertions are
mechanical.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip(
    "cryptography",
    reason="cryptography not installed — install with: uv add 'movate-cli[github]'",
)

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from movate.integrations.github import (
    CommitInfo,
    GitHubClient,
    GitHubConfig,
    GitHubError,
    PublishResult,
    is_enabled,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _gen_rsa_pem() -> str:
    """Generate a fresh RSA-2048 private key + return its PEM-encoded
    text. Fast enough to call per-test on modern hardware."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


@pytest.fixture
def rsa_pem() -> str:
    """Per-test RSA key — hermetic + matches what an operator hands the
    GitHub App at registration."""
    return _gen_rsa_pem()


@pytest.fixture
def config(rsa_pem: str) -> GitHubConfig:
    return GitHubConfig(
        app_id=12345,
        installation_id=67890,
        private_key_pem=rsa_pem,
        repo="acme-org/mova-io-agents-acme",
        default_branch="main",
        commit_author_name="Mova iO",
        commit_author_email="noreply@mova-io.movate.com",
    )


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    """A minimal canonical bundle to publish. Mirrors the on-disk
    layout `persist_bundle` produces for a freshly-created agent."""
    root = tmp_path / "faq-bot"
    root.mkdir()
    (root / "agent.yaml").write_text("name: faq-bot\nversion: 1.0.0\n")
    (root / "prompt.md").write_text("You are a helpful FAQ bot.\n")
    schema_dir = root / "schema"
    schema_dir.mkdir()
    (schema_dir / "input.json").write_text('{"type": "object"}')
    (schema_dir / "output.json").write_text('{"type": "object"}')
    # Dot-prefixed file MUST NOT be committed (registry/git hygiene).
    (root / ".DS_Store").write_bytes(b"mac noise")
    return root


# ---------------------------------------------------------------------------
# Recording mock transport
# ---------------------------------------------------------------------------


class RecordingHandler:
    """Records every request + returns canned responses by URL pattern."""

    def __init__(self, responses: dict[tuple[str, str], httpx.Response]):
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # Most-specific match first: try exact (method, full URL)
        # then (method, URL with `*` suffix wildcard).
        full_key = (request.method, str(request.url))
        if full_key in self.responses:
            return self.responses[full_key]
        # Fall back to path-only match (host-agnostic).
        path_key = (request.method, request.url.path)
        if path_key in self.responses:
            return self.responses[path_key]
        raise AssertionError(
            f"unexpected request: {request.method} {request.url} "
            f"(known: {list(self.responses.keys())})"
        )


def _mock_client(handler: RecordingHandler) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient wired to the recording mock transport."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("garbage", False),
    ],
)
def test_is_enabled_truthiness(value: str, expected: bool) -> None:
    assert is_enabled({"MDK_GITHUB_ENABLED": value}) is expected


def test_is_enabled_default_off() -> None:
    """Empty env = off. Item 78 ships with the integration disabled so
    the live runtime doesn't change behavior on the next deploy."""
    assert is_enabled({}) is False


# ---------------------------------------------------------------------------
# GitHubConfig.from_env
# ---------------------------------------------------------------------------


def test_config_from_env_happy_path(rsa_pem: str) -> None:
    cfg = GitHubConfig.from_env(
        {
            "MDK_GITHUB_APP_ID": "111",
            "MDK_GITHUB_INSTALLATION_ID": "222",
            "MDK_GITHUB_PRIVATE_KEY": rsa_pem,
            "MDK_GITHUB_REPO": "owner/repo",
        }
    )
    assert cfg.app_id == 111
    assert cfg.installation_id == 222
    assert cfg.repo == "owner/repo"
    assert cfg.default_branch == "main"  # default
    assert cfg.commit_author_name == "Mova iO"


def test_config_from_env_missing_var_raises_422(rsa_pem: str) -> None:
    """Operator forgets one of the required env vars — we error with a
    pointer at the missing one + ADR 007 schema reference."""
    with pytest.raises(GitHubError) as excinfo:
        GitHubConfig.from_env(
            {
                "MDK_GITHUB_APP_ID": "111",
                # INSTALLATION_ID missing on purpose
                "MDK_GITHUB_PRIVATE_KEY": rsa_pem,
                "MDK_GITHUB_REPO": "owner/repo",
            }
        )
    assert excinfo.value.status_code == 422
    assert "MDK_GITHUB_INSTALLATION_ID" in str(excinfo.value)


def test_config_from_env_non_int_id_raises_422(rsa_pem: str) -> None:
    with pytest.raises(GitHubError) as excinfo:
        GitHubConfig.from_env(
            {
                "MDK_GITHUB_APP_ID": "not-a-number",
                "MDK_GITHUB_INSTALLATION_ID": "222",
                "MDK_GITHUB_PRIVATE_KEY": rsa_pem,
                "MDK_GITHUB_REPO": "owner/repo",
            }
        )
    assert excinfo.value.status_code == 422


# ---------------------------------------------------------------------------
# JWT signing + installation-token exchange
# ---------------------------------------------------------------------------


async def test_installation_token_exchange_happy(
    config: GitHubConfig,
) -> None:
    """The first authenticated API call mints an installation token
    via the App JWT. Body shape MUST match the GitHub Apps spec."""
    handler = RecordingHandler(
        {
            (
                "POST",
                "/app/installations/67890/access_tokens",
            ): httpx.Response(
                201,
                json={
                    "token": "ghs_installation_token_abcd",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            ),
            ("GET", "/repos/acme-org/mova-io-agents-acme/git/blobs"): httpx.Response(200, json={}),
        }
    )

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        token = await client._get_installation_token()
        assert token == "ghs_installation_token_abcd"

    # The auth header MUST carry the freshly-signed App JWT
    # (Bearer <header>.<payload>.<sig>). Decode the payload and
    # assert iss = app_id.
    req = next(r for r in handler.requests if r.url.path.endswith("/access_tokens"))
    auth = req.headers["authorization"]
    assert auth.startswith("Bearer ")
    jwt = auth.removeprefix("Bearer ")
    header_b64, payload_b64, _sig = jwt.split(".")
    # Re-pad base64url before decoding.
    padded = payload_b64 + "=" * ((4 - len(payload_b64) % 4) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    assert payload["iss"] == "12345"
    # Header must declare RS256.
    padded_h = header_b64 + "=" * ((4 - len(header_b64) % 4) % 4)
    header = json.loads(base64.urlsafe_b64decode(padded_h))
    assert header["alg"] == "RS256"


async def test_installation_token_exchange_failure_502(
    config: GitHubConfig,
) -> None:
    """GitHub returns 401 (e.g. wrong installation_id) → we surface a
    502-tagged GitHubError with the upstream status preserved so the
    caller can debug."""
    handler = RecordingHandler(
        {
            (
                "POST",
                "/app/installations/67890/access_tokens",
            ): httpx.Response(401, text='{"message": "Bad credentials"}'),
        }
    )

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        with pytest.raises(GitHubError) as excinfo:
            await client._get_installation_token()
        assert excinfo.value.status_code == 502
        assert excinfo.value.upstream_status == 401


async def test_installation_token_is_cached_within_ttl(
    config: GitHubConfig,
) -> None:
    """Back-to-back authenticated calls share one installation token —
    we should NOT re-exchange the JWT every request (5000/h limit)."""
    handler = RecordingHandler(
        {
            (
                "POST",
                "/app/installations/67890/access_tokens",
            ): httpx.Response(
                201,
                json={
                    "token": "ghs_one_time",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            ),
        }
    )

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        t1 = await client._get_installation_token()
        t2 = await client._get_installation_token()
        assert t1 == t2

    # Only ONE access-token exchange happened — not two.
    exchanges = [r for r in handler.requests if r.url.path.endswith("/access_tokens")]
    assert len(exchanges) == 1


# ---------------------------------------------------------------------------
# publish_bundle — the 5-step Git Data API dance
# ---------------------------------------------------------------------------


def _publish_responses() -> dict[tuple[str, str], httpx.Response]:
    """Canned responses for a happy publish. Mocks every endpoint the
    5-step dance touches, returning realistic-shape payloads."""
    return {
        ("POST", "/app/installations/67890/access_tokens"): httpx.Response(
            201,
            json={
                "token": "ghs_token",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        ),
        # Step 1 — blobs (one per file). Returning the same sha for all
        # is OK — the tree creator uses each entry's sha individually.
        (
            "POST",
            "/repos/acme-org/mova-io-agents-acme/git/blobs",
        ): httpx.Response(201, json={"sha": "blob-sha-deadbeef"}),
        # Step 2 — head ref → commit → tree.
        (
            "GET",
            "/repos/acme-org/mova-io-agents-acme/git/ref/heads/main",
        ): httpx.Response(
            200,
            json={"object": {"sha": "head-commit-sha"}},
        ),
        (
            "GET",
            "/repos/acme-org/mova-io-agents-acme/git/commits/head-commit-sha",
        ): httpx.Response(
            200,
            json={"tree": {"sha": "head-tree-sha"}},
        ),
        # Step 3 — new tree.
        (
            "POST",
            "/repos/acme-org/mova-io-agents-acme/git/trees",
        ): httpx.Response(201, json={"sha": "new-tree-sha"}),
        # Step 4 — new commit.
        (
            "POST",
            "/repos/acme-org/mova-io-agents-acme/git/commits",
        ): httpx.Response(201, json={"sha": "new-commit-sha-cafef00d"}),
        # Step 5 — fast-forward.
        (
            "PATCH",
            "/repos/acme-org/mova-io-agents-acme/git/refs/heads/main",
        ): httpx.Response(200, json={"ref": "refs/heads/main"}),
    }


async def test_publish_bundle_happy_path(config: GitHubConfig, bundle_dir: Path) -> None:
    """End-to-end publish: lands on every Git Data API endpoint in the
    right order with the right bodies, returns a PublishResult with the
    new commit SHA + URL."""
    handler = RecordingHandler(_publish_responses())

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        result = await client.publish_bundle(
            bundle_dir,
            target_dir="faq-bot",
            message="Initial publish",
        )

    assert isinstance(result, PublishResult)
    assert result.commit_sha == "new-commit-sha-cafef00d"
    assert result.commit_url == (
        "https://github.com/acme-org/mova-io-agents-acme/commit/new-commit-sha-cafef00d"
    )
    assert result.branch == "main"
    # The bundle has 4 real files; the dot-prefixed .DS_Store must NOT
    # be committed.
    expected_paths = {
        "faq-bot/agent.yaml",
        "faq-bot/prompt.md",
        "faq-bot/schema/input.json",
        "faq-bot/schema/output.json",
    }
    assert set(result.files_changed) == expected_paths

    # Verify ordering — by request URLs in arrival order. The first
    # call is the access-token exchange; then 4 blobs; then ref →
    # commit → trees → commits → ref PATCH.
    paths = [r.url.path for r in handler.requests]
    assert paths[0].endswith("/access_tokens")
    blob_indices = [i for i, p in enumerate(paths) if p.endswith("/git/blobs")]
    assert len(blob_indices) == 4  # 4 files in the bundle (no .DS_Store)
    # Trees comes AFTER blobs.
    assert paths.index("/repos/acme-org/mova-io-agents-acme/git/trees") > max(blob_indices)
    # Ref-update is the LAST request.
    assert paths[-1].endswith("/git/refs/heads/main")


async def test_publish_bundle_sends_base_tree(config: GitHubConfig, bundle_dir: Path) -> None:
    """The trees POST must include ``base_tree`` so unrelated files in
    the repo stay put. Without this, the new commit would wipe every
    file outside ``target_dir/``."""
    handler = RecordingHandler(_publish_responses())

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        await client.publish_bundle(bundle_dir, target_dir="faq-bot", message="x")

    trees_req = next(r for r in handler.requests if r.url.path.endswith("/git/trees"))
    body: dict[str, Any] = json.loads(trees_req.content)
    assert body["base_tree"] == "head-tree-sha"
    # All tree entries are blobs with 100644 mode.
    for entry in body["tree"]:
        assert entry["mode"] == "100644"
        assert entry["type"] == "blob"


async def test_publish_bundle_uses_custom_author(config: GitHubConfig, bundle_dir: Path) -> None:
    """When the caller passes ``author_name`` / ``author_email``, they
    override the config defaults — lets the Angular UI attribute
    commits to a per-user identity (v0.8 SSO integration)."""
    handler = RecordingHandler(_publish_responses())

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        await client.publish_bundle(
            bundle_dir,
            target_dir="faq-bot",
            message="Custom author publish",
            author_name="Deva",
            author_email="deva@movate.com",
        )

    commits_req = next(
        r for r in handler.requests if r.method == "POST" and r.url.path.endswith("/git/commits")
    )
    body = json.loads(commits_req.content)
    assert body["author"]["name"] == "Deva"
    assert body["author"]["email"] == "deva@movate.com"
    assert body["message"] == "Custom author publish"


async def test_publish_bundle_missing_dir_raises_404(config: GitHubConfig, tmp_path: Path) -> None:
    handler = RecordingHandler({})  # zero calls expected
    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        with pytest.raises(GitHubError) as excinfo:
            await client.publish_bundle(
                tmp_path / "does-not-exist",
                target_dir="ghost",
                message="x",
            )
        assert excinfo.value.status_code == 404


async def test_publish_bundle_empty_dir_raises_422(config: GitHubConfig, tmp_path: Path) -> None:
    empty = tmp_path / "empty-agent"
    empty.mkdir()
    handler = RecordingHandler({})
    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        with pytest.raises(GitHubError) as excinfo:
            await client.publish_bundle(empty, target_dir="empty-agent", message="x")
        assert excinfo.value.status_code == 422


async def test_publish_bundle_upstream_failure_502(config: GitHubConfig, bundle_dir: Path) -> None:
    """If GitHub returns 500 on the trees POST, we surface it as a
    502 GitHubError with the upstream status preserved."""
    responses = _publish_responses()
    responses[("POST", "/repos/acme-org/mova-io-agents-acme/git/trees")] = httpx.Response(
        500, text='{"message": "internal"}'
    )
    handler = RecordingHandler(responses)

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        with pytest.raises(GitHubError) as excinfo:
            await client.publish_bundle(bundle_dir, target_dir="faq-bot", message="x")
        assert excinfo.value.status_code == 502
        assert excinfo.value.upstream_status == 500


# ---------------------------------------------------------------------------
# list_history (item 79)
# ---------------------------------------------------------------------------


def _two_commit_response() -> httpx.Response:
    """Canned GitHub commits-list response with two rows in the
    real shape the API returns. Used by the happy-path test +
    pagination test."""
    return httpx.Response(
        200,
        json=[
            {
                "sha": "abc123" + "0" * 34,
                "html_url": (
                    "https://github.com/acme-org/mova-io-agents-acme/commit/abc123" + "0" * 34
                ),
                "commit": {
                    "message": "Tighten the system prompt",
                    "author": {
                        "name": "Deva",
                        "email": "deva@movate.com",
                        "date": "2026-05-14T09:00:00Z",
                    },
                },
            },
            {
                "sha": "def456" + "0" * 34,
                "html_url": (
                    "https://github.com/acme-org/mova-io-agents-acme/commit/def456" + "0" * 34
                ),
                "commit": {
                    "message": "Initial publish",
                    "author": {
                        "name": "Mova iO",
                        "email": "noreply@mova-io.movate.com",
                        "date": "2026-05-13T18:00:00Z",
                    },
                },
            },
        ],
    )


async def test_list_history_happy_path(config: GitHubConfig) -> None:
    """Two commits → two CommitInfo rows with the fields flattened
    out of GitHub's nested response shape."""
    handler = RecordingHandler(
        {
            ("POST", "/app/installations/67890/access_tokens"): httpx.Response(
                201,
                json={"token": "ghs_t", "expires_at": "2099-01-01T00:00:00Z"},
            ),
            ("GET", "/repos/acme-org/mova-io-agents-acme/commits"): _two_commit_response(),
        }
    )

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        rows = await client.list_history(target_dir="faq-bot", limit=50, page=1)

    assert len(rows) == 2
    assert all(isinstance(r, CommitInfo) for r in rows)
    assert rows[0].sha.startswith("abc123")
    assert rows[0].message == "Tighten the system prompt"
    assert rows[0].author_name == "Deva"
    assert rows[0].author_email == "deva@movate.com"
    assert rows[0].timestamp == "2026-05-14T09:00:00Z"
    assert rows[0].html_url.endswith(rows[0].sha)


async def test_list_history_sends_path_filter_and_pagination(config: GitHubConfig) -> None:
    """The commits endpoint must be called with ?path=<dir>&per_page&page
    so GitHub filters by agent directory + paginates correctly. Without
    the path filter we'd get every commit in the repo across every agent."""
    handler = RecordingHandler(
        {
            ("POST", "/app/installations/67890/access_tokens"): httpx.Response(
                201,
                json={"token": "ghs_t", "expires_at": "2099-01-01T00:00:00Z"},
            ),
            ("GET", "/repos/acme-org/mova-io-agents-acme/commits"): _two_commit_response(),
        }
    )

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        await client.list_history(target_dir="faq-bot/", limit=25, page=3)

    commits_req = next(r for r in handler.requests if r.url.path.endswith("/commits"))
    qs = dict(commits_req.url.params)
    assert qs["path"] == "faq-bot"  # trailing slash stripped
    assert qs["per_page"] == "25"
    assert qs["page"] == "3"


async def test_list_history_clamps_limit_to_100(config: GitHubConfig) -> None:
    """GitHub rejects per_page > 100. We clamp client-side so a UI
    that asks for 500 still gets a 200 response (with the first 100)."""
    handler = RecordingHandler(
        {
            ("POST", "/app/installations/67890/access_tokens"): httpx.Response(
                201,
                json={"token": "ghs_t", "expires_at": "2099-01-01T00:00:00Z"},
            ),
            ("GET", "/repos/acme-org/mova-io-agents-acme/commits"): httpx.Response(200, json=[]),
        }
    )

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        await client.list_history(target_dir="x", limit=500, page=1)

    commits_req = next(r for r in handler.requests if r.url.path.endswith("/commits"))
    qs = dict(commits_req.url.params)
    assert qs["per_page"] == "100"  # clamped down from 500


async def test_list_history_empty_returns_empty_list(config: GitHubConfig) -> None:
    """An agent that's never been published returns 200 with []. We
    surface that as an empty list, NOT a 404 — 'no commits' is a valid
    state for a freshly-created bundle."""
    handler = RecordingHandler(
        {
            ("POST", "/app/installations/67890/access_tokens"): httpx.Response(
                201,
                json={"token": "ghs_t", "expires_at": "2099-01-01T00:00:00Z"},
            ),
            ("GET", "/repos/acme-org/mova-io-agents-acme/commits"): httpx.Response(200, json=[]),
        }
    )

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        rows = await client.list_history(target_dir="never-published")
    assert rows == []


async def test_list_history_tolerates_missing_fields(config: GitHubConfig) -> None:
    """Real-world GitHub responses sometimes lack author email (anonymous
    commits, mirror imports). We fall back to empty strings instead of
    crashing the response."""
    handler = RecordingHandler(
        {
            ("POST", "/app/installations/67890/access_tokens"): httpx.Response(
                201,
                json={"token": "ghs_t", "expires_at": "2099-01-01T00:00:00Z"},
            ),
            ("GET", "/repos/acme-org/mova-io-agents-acme/commits"): httpx.Response(
                200,
                json=[
                    {
                        "sha": "abc",
                        # No html_url, no commit.author block
                        "commit": {"message": "anonymous commit"},
                    }
                ],
            ),
        }
    )

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        rows = await client.list_history(target_dir="x")

    assert len(rows) == 1
    assert rows[0].sha == "abc"
    assert rows[0].message == "anonymous commit"
    assert rows[0].author_name == ""
    assert rows[0].author_email == ""
    assert rows[0].timestamp == ""
    assert rows[0].html_url == ""


async def test_list_history_upstream_502(config: GitHubConfig) -> None:
    """GitHub returns 500 on the commits call → we surface a 502
    GitHubError with the upstream status preserved."""
    handler = RecordingHandler(
        {
            ("POST", "/app/installations/67890/access_tokens"): httpx.Response(
                201,
                json={"token": "ghs_t", "expires_at": "2099-01-01T00:00:00Z"},
            ),
            ("GET", "/repos/acme-org/mova-io-agents-acme/commits"): httpx.Response(
                500, text="boom"
            ),
        }
    )

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        with pytest.raises(GitHubError) as excinfo:
            await client.list_history(target_dir="faq-bot")
        assert excinfo.value.status_code == 502
        assert excinfo.value.upstream_status == 500


async def test_list_history_non_array_response_is_502(config: GitHubConfig) -> None:
    """If GitHub ever returns a dict instead of an array (shouldn't happen
    but we defend), we surface a 502 instead of crashing the response
    handler with a TypeError."""
    handler = RecordingHandler(
        {
            ("POST", "/app/installations/67890/access_tokens"): httpx.Response(
                201,
                json={"token": "ghs_t", "expires_at": "2099-01-01T00:00:00Z"},
            ),
            ("GET", "/repos/acme-org/mova-io-agents-acme/commits"): httpx.Response(
                200, json={"unexpected": "object"}
            ),
        }
    )

    async with _mock_client(handler) as http:
        client = GitHubClient(config, http=http)
        with pytest.raises(GitHubError) as excinfo:
            await client.list_history(target_dir="x")
        assert excinfo.value.status_code == 502
