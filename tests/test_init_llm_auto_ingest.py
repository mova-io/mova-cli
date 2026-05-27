"""F7 (#116) — ``mdk init --llm`` auto crawl+ingest on URL intent.

Closes the loop: ``mdk init bot "answer questions about https://movate.com"
--llm`` scaffolds a RAG agent AND populates its KB from that site, so the
grounded agent can actually answer on the first run.

Behavior under test:

* ``--llm`` with a URL description (mock the LLM scaffolder via ``--mock``
  is OFF here — we need the real scaffold shape but a stubbed network) →
  the RAG agent is scaffolded AND chunks land in its KB from the URL.
* No embedding key → scaffold succeeds, auto-ingest is skipped, the manual
  ``mdk kb ingest`` hint is printed (exit 0).
* ``--mock`` → offline, scaffold-only, hint printed, NO network.
* ``--no-ingest`` → scaffold only, no ingest attempted (no network).
* URL-less grounding description → RAG scaffold, no auto-ingest, manual hint.
* Crawl error / empty → scaffold succeeds, warn + hint (exit 0).

These tests are hermetic — NO real network. ``httpx.get`` is monkeypatched
to serve fixture HTML (so the real ``crawl_site`` → ``ingest_text`` path
runs end to end) and ``embed_texts`` is stubbed. The scaffold itself uses
the offline ``--mock`` provider for the RAG shape; for the network-exercising
cases we drive a real scaffold via the mock provider but stub the embed +
fetch so no key + no traffic is needed. Storage routes to a tmp sqlite file
via ``MOVATE_DB`` (no ~/.movate writes).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.kb.web import first_url
from movate.scaffold import GeneratedAgent, GenerationResult

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PROSE = (
    "Movate builds enterprise AI agents. This paragraph is long enough to "
    "clear the minimum chunk size threshold so it becomes its own chunk."
)
_PROSE2 = (
    "A second substantial paragraph about the product that also exceeds the "
    "minimum chunk length, giving the crawler a second retrievable chunk."
)

_PAGE_HTML = f"""\
<!DOCTYPE html>
<html>
<head><title>Movate</title></head>
<body>
  <article>
    <h1>About Movate</h1>
    <p>{_PROSE}</p>
    <p>{_PROSE2}</p>
  </article>
</body>
</html>
"""


def _fake_response(text: str, status_code: int = 200) -> httpx.Response:
    """An httpx.Response detached from any real transport."""
    return httpx.Response(
        status_code=status_code,
        text=text,
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", "https://movate.com"),
    )


async def _fake_embed(
    texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
) -> list[list[float]]:
    """Deterministic stub — no embedding-provider traffic."""
    return [[float(len(t) % 7), 1.0, 0.0, 0.5] for t in texts]


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route storage to a tmp sqlite file + provide an embedding key.

    ``MOVATE_DB`` overrides ``~/.movate/local.db`` so the CLI never writes
    to the developer's home dir.
    """
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "kb.db"))
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")


def _rag_generation_result(name: str) -> GenerationResult:
    """A valid RAG-shaped GenerationResult, reusing the mock's payload.

    Lets the network-exercising tests drive a real (non --mock) scaffold
    without a live LLM: we patch ``generate_agent_from_description`` to
    return this, so the CLI writes a genuine RAG agent and then proceeds
    to the auto-ingest step (which --mock would otherwise short-circuit).
    """
    import json  # noqa: PLC0415

    from movate.core.models import TokenUsage  # noqa: PLC0415
    from movate.providers.mock import _build_scaffold_response  # noqa: PLC0415

    payload = json.loads(_build_scaffold_response(name, grounding=True))
    return GenerationResult(agent=GeneratedAgent.model_validate(payload), tokens=TokenUsage())


def _patch_scaffolder(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Patch the scaffolder to return a deterministic RAG result (no LLM)."""

    async def _fake_generate(**kwargs: object) -> GenerationResult:
        return _rag_generation_result(name)

    monkeypatch.setattr("movate.scaffold.generate_agent_from_description", _fake_generate)


def _read_chunks(agent: str) -> list[object]:
    """Read every chunk for ``agent`` from the configured storage backend."""
    import asyncio  # noqa: PLC0415

    from movate.storage import build_storage  # noqa: PLC0415

    async def _go() -> list[object]:
        storage = build_storage()
        await storage.init()
        try:
            return await storage.list_kb_chunks(agent=agent, tenant_id="local")
        finally:
            await storage.close()

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# Unit: URL extraction from a free-text description
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFirstUrl:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("answer questions about https://movate.com", "https://movate.com"),
            ("based on http://example.com/docs please", "http://example.com/docs"),
            # Trailing sentence punctuation is not part of the URL.
            ("see https://movate.com.", "https://movate.com"),
            ("(https://movate.com/faq)", "https://movate.com/faq"),
            # First URL wins when several are present.
            ("a https://one.example and https://two.example", "https://one.example"),
        ],
    )
    def test_extracts_embedded_url(self, text: str, expected: str) -> None:
        assert first_url(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "answer questions about our help docs",
            "classify text into sentiment labels",
            "summarize a block of text",
            "",
            "ftp://example.com is not http(s)",
        ],
    )
    def test_no_url_returns_none(self, text: str) -> None:
        assert first_url(text) is None


# ---------------------------------------------------------------------------
# CLI end-to-end — auto-ingest the URL into the new RAG agent's KB
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoIngestHappyPath:
    def test_url_description_scaffolds_and_ingests(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A URL in the description → RAG agent scaffolded AND its KB
        populated by crawling that URL (single fixture page, no links).

        Drives a real (non --mock) scaffold so auto-ingest runs, but the
        LLM scaffolder is patched to a deterministic RAG result and the
        network + embedding calls are stubbed — fully hermetic.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response(_PAGE_HTML))
        _patch_scaffolder(monkeypatch, "site-bot")
        url = "https://movate.com"

        with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
            result = runner.invoke(
                app,
                [
                    "init",
                    "--bare",
                    "site-bot",
                    "--llm",
                    f"answer questions about {url}",
                    "--target",
                    str(tmp_path),
                ],
            )

        # The agent scaffolded as a RAG shape.
        assert result.exit_code == 0, result.stdout + result.stderr
        spec = yaml.safe_load((tmp_path / "site-bot" / "agent.yaml").read_text())
        assert spec["skills"] == ["kb-vector-lookup"]
        assert spec["retrieval"]["auto_into"] == "context"

        # Chunks landed in the agent's KB with source == the URL.
        chunks = _read_chunks("site-bot")
        assert chunks, "expected chunks auto-ingested from the URL"
        assert all(c.source == url for c in chunks)  # type: ignore[attr-defined]
        joined = "\n".join(c.text for c in chunks)  # type: ignore[attr-defined]
        assert "enterprise AI agents" in joined

        # The grounded-on-N-pages confirmation is printed.
        assert "grounded on" in result.stdout
        assert url in result.stdout


# ---------------------------------------------------------------------------
# CLI end-to-end — best-effort: never breaks init
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoIngestNeverBreaksInit:
    def test_no_embedding_key_skips_ingest_with_hint(
        self,
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No embedding key (real scaffold) → scaffold succeeds, auto-ingest
        skipped, manual hint printed, exit 0, NO chunks written."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MOVATE_DB", str(tmp_path / "kb.db"))
        monkeypatch.delenv("MOVATE_DB_URL", raising=False)
        # No OpenAI (embedding) key — that's what the auto-ingest key check
        # gates on. The scaffold path needs SOME provider key to clear its
        # own pre-flight; give it a non-OpenAI one so the embedding-key
        # branch is what's exercised. The scaffolder is patched so no real
        # LLM call fires regardless.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")
        _patch_scaffolder(monkeypatch, "kb-bot")
        url = "https://movate.com"

        def _should_not_fetch(*a: object, **k: object) -> httpx.Response:
            raise AssertionError("no-key path must skip BEFORE any fetch")

        monkeypatch.setattr(httpx, "get", _should_not_fetch)

        result = runner.invoke(
            app,
            # --bare keeps the standalone <tmp>/kb-bot/ layout this assertion
            # targets (ADR 026 D1 default would wrap it in a project).
            [
                "init",
                "kb-bot",
                "--llm",
                f"answer questions about {url}",
                "--bare",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Scaffold is intact.
        assert (tmp_path / "kb-bot" / "agent.yaml").is_file()
        # Nothing ingested (no embedding key).
        assert _read_chunks("kb-bot") == []
        # Manual hint + the missing-key reason.
        assert "OPENAI_API_KEY" in result.stderr
        assert "mdk kb ingest" in result.stderr

    def test_auto_ingest_url_no_key_raises_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unit: the reusable helper raises AutoIngestSkippedError (never a
        bare exception / typer.Exit) when the embedding key is absent."""
        import asyncio  # noqa: PLC0415

        from movate.cli import kb_cmd  # noqa: PLC0415

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(kb_cmd.AutoIngestSkippedError) as exc:
            asyncio.run(
                kb_cmd.auto_ingest_url(
                    agent="kb-bot", url="https://movate.com", project_root=tmp_path
                )
            )
        assert "OPENAI_API_KEY" in str(exc.value)

    def test_crawl_empty_skips_with_warning(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Crawl finds nothing ingestible → scaffold succeeds, warn + hint,
        exit 0, no chunks written."""
        monkeypatch.chdir(tmp_path)
        # An empty-body page yields no extractable prose → crawl_site
        # records it as skipped and returns zero pages.
        monkeypatch.setattr(
            httpx, "get", lambda *a, **k: _fake_response("<html><body></body></html>")
        )
        _patch_scaffolder(monkeypatch, "empty-bot")
        url = "https://movate.com"

        with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
            result = runner.invoke(
                app,
                [
                    "init",
                    "--bare",
                    "empty-bot",
                    "--llm",
                    f"answer questions about {url}",
                    "--target",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "empty-bot" / "agent.yaml").is_file()
        assert _read_chunks("empty-bot") == []
        assert "auto-ingest skipped" in result.stderr
        assert "mdk kb ingest" in result.stderr

    def test_crawl_unreachable_skips_with_warning(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Start URL unreachable → crawl yields nothing → scaffold stands,
        warn + hint, exit 0."""
        monkeypatch.chdir(tmp_path)

        def _boom(*a: object, **k: object) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "get", _boom)
        _patch_scaffolder(monkeypatch, "down-bot")
        url = "https://movate.com"

        with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
            result = runner.invoke(
                app,
                [
                    "init",
                    "--bare",
                    "down-bot",
                    "--llm",
                    f"answer questions about {url}",
                    "--target",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "down-bot" / "agent.yaml").is_file()
        assert _read_chunks("down-bot") == []
        assert "mdk kb ingest" in result.stderr


# ---------------------------------------------------------------------------
# CLI end-to-end — opt-outs + no-URL + non-grounding
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoIngestOptOuts:
    def test_no_ingest_flag_skips_network(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--no-ingest → scaffold only, NO network call, hint printed.

        Real (non --mock) scaffold so we prove --no-ingest itself blocks
        the network — not the --mock short-circuit."""
        monkeypatch.chdir(tmp_path)
        _patch_scaffolder(monkeypatch, "scaffold-only")

        def _should_not_fetch(*a: object, **k: object) -> httpx.Response:
            raise AssertionError("--no-ingest must not hit the network")

        monkeypatch.setattr(httpx, "get", _should_not_fetch)
        url = "https://movate.com"

        with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
            result = runner.invoke(
                app,
                [
                    "init",
                    "--bare",
                    "scaffold-only",
                    "--llm",
                    f"answer questions about {url}",
                    "--no-ingest",
                    "--target",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "scaffold-only" / "agent.yaml").is_file()
        assert _read_chunks("scaffold-only") == []
        assert "--no-ingest" in result.stderr
        assert "mdk kb ingest" in result.stderr

    def test_mock_skips_network_scaffold_only(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--mock → offline, no network, scaffold only, hint printed."""
        monkeypatch.chdir(tmp_path)

        def _should_not_fetch(*a: object, **k: object) -> httpx.Response:
            raise AssertionError("--mock must stay offline")

        monkeypatch.setattr(httpx, "get", _should_not_fetch)
        url = "https://movate.com"

        result = runner.invoke(
            app,
            [
                "init",
                "--bare",
                "mock-bot",
                "--llm",
                f"answer questions about {url}",
                "--mock",
                "--target",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "mock-bot" / "agent.yaml").is_file()
        assert _read_chunks("mock-bot") == []
        assert "--mock" in result.stderr
        assert "mdk kb ingest" in result.stderr

    def test_urlless_grounding_scaffolds_without_ingest(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A grounding description with NO URL → RAG scaffold, no auto-ingest,
        manual hint (KB starts empty). Driven via a real (non --mock)
        scaffold (patched scaffolder) so the URL-less branch — not the
        --mock short-circuit — is the thing under test."""
        monkeypatch.chdir(tmp_path)
        _patch_scaffolder(monkeypatch, "docs-bot")

        def _should_not_fetch(*a: object, **k: object) -> httpx.Response:
            raise AssertionError("a URL-less description must not fetch")

        monkeypatch.setattr(httpx, "get", _should_not_fetch)

        with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
            result = runner.invoke(
                app,
                [
                    "init",
                    "--bare",
                    "docs-bot",
                    "--llm",
                    "answer questions about our help docs",
                    "--target",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, result.stdout + result.stderr
        spec = yaml.safe_load((tmp_path / "docs-bot" / "agent.yaml").read_text())
        assert spec["skills"] == ["kb-vector-lookup"]
        assert _read_chunks("docs-bot") == []
        assert "empty" in result.stderr
        assert "mdk kb ingest" in result.stderr

    def test_non_grounding_scaffold_no_ingest_no_hint(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-grounding description → no RAG block, NO auto-ingest, NO
        network call. Uses --mock so the deterministic mock provider emits
        the non-grounding shape offline (no key needed)."""
        monkeypatch.chdir(tmp_path)

        def _should_not_fetch(*a: object, **k: object) -> httpx.Response:
            raise AssertionError("a non-grounding scaffold must not fetch")

        monkeypatch.setattr(httpx, "get", _should_not_fetch)

        result = runner.invoke(
            app,
            [
                "init",
                "--bare",
                "sentiment",
                "--llm",
                "classify short text into sentiment labels",
                "--mock",
                "--target",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        spec = yaml.safe_load((tmp_path / "sentiment" / "agent.yaml").read_text())
        assert "skills" not in spec
        assert "retrieval" not in spec
        assert _read_chunks("sentiment") == []
