"""D7a (#132) — hermetic end-to-end acceptance guard for the ``--llm``
URL→RAG slice.

This is the *acceptance guard* for the whole ``mdk init <name> "answer
questions about <url>" --llm`` chain. Where the per-feature suites
(``test_init_llm_rag_scaffold.py`` F3, ``test_kb_site_crawl.py`` F6,
``test_init_llm_auto_ingest.py`` F7, ``test_init_llm_grounded_verify.py``
F8) each test one stage in isolation, this exercises the ENTIRE chain in a
SINGLE ``mdk init`` invocation and asserts every stage, so a regression in
*any* of F3 / F5 / F6 / F7 / F8 — or in the canonical agent layout (#127) —
fails this one test.

The slice, in one command::

    mdk init site-bot "answer questions about https://example.test/docs"

  F3  grounding intent (the embedded URL) → a RAG-shaped scaffold in the
      canonical layout (agent.yaml + prompt.md + schema/{input,output}.yaml +
      evals/{dataset.jsonl, judge.yaml.example}); skills: [kb-vector-lookup],
      retrieval.auto_into: context.
  F6  the URL is crawled (BFS, same-site, bounded) — F5 (``kb/web.py``)
      fetches each page; F6 (``crawl_site``) follows same-site links.
  F7  the crawl is auto-ingested into the new agent's KB (chunk → embed →
      save_kb_chunk), each chunk's ``source`` == its own page URL.
  F8  a grounded probe runs THROUGH the agent (the real ``Executor.execute``
      path with ADR-023 pre-retrieval against the seeded KB) and reports
      ``✓ verified``.

Hermetic — NO API keys, NO real network, NO ``~/.movate`` writes:

* the LLM scaffolder is patched to a deterministic RAG payload (the mock
  provider's grounding shape) so no model call fires;
* ``httpx.get`` serves a tiny in-memory *same-site link graph* (a start page
  linking to a sub-page) so the real ``crawl_site`` → ``ingest_text`` path
  runs end to end and we can prove the crawl FOLLOWED a link;
* ``embed_texts`` is stubbed at BOTH call-sites (ingest + search) with a
  fixed vector so the seeded chunks are retrievable by the probe query;
* storage routes to a tmp sqlite file via ``MOVATE_DB``;
* the F8 verify probe's ``build_local_runtime`` is patched to a controlled
  ``MockProvider`` emitting a grounded answer — the genuine
  ``Executor.execute`` (input/output schema validation + ADR-023
  pre-retrieval) still runs against the real seeded KB.

All stubs are the SAME ones the per-feature suites use; this module just
wires them together for the one-shot slice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.loader import load_agent
from movate.scaffold import GeneratedAgent, GenerationResult

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# A tiny same-site link graph: start page → /docs/intro (depth 1).
#
# Auto-ingest crawls with max_depth=1, so the start page AND its direct
# same-site links are fetched. A linked sub-page (different URL) lets us
# prove the crawl actually FOLLOWED a link, not just fetched the seed —
# i.e. F6 (crawl_site) ran, not only F5 (single-page fetch).
# ---------------------------------------------------------------------------

_START_URL = "https://example.test/docs"
_SUB_URL = "https://example.test/docs/intro"
_OFFSITE_URL = "https://external.example/x"  # never followed (same-site guard)

_START_PROSE = (
    "Movate builds enterprise AI agents for regulated industries. This "
    "paragraph is comfortably long enough to clear the minimum chunk size "
    "threshold so it becomes its own retrievable chunk on the start page."
)
_SUB_PROSE = (
    "The onboarding guide explains how to scaffold, evaluate, and deploy an "
    "agent. This second page's prose also exceeds the minimum chunk length, "
    "so the crawler produces a distinct chunk sourced to the sub-page URL."
)


def _page_html(title: str, prose: str, links: list[str]) -> str:
    anchors = "\n".join(f'<a href="{href}">{href}</a>' for href in links)
    return (
        "<!DOCTYPE html><html><head><title>"
        f"{title}</title></head><body><article>"
        f"<h1>{title}</h1><p>{prose}</p>"
        f"<nav>{anchors}</nav>"
        "</article></body></html>"
    )


_SITE: dict[str, str] = {
    # Start page links to a same-site sub-page (followed) and an off-site
    # page (NOT followed by the same-site guard).
    _START_URL: _page_html("Docs", _START_PROSE, ["/docs/intro", _OFFSITE_URL]),
    _SUB_URL: _page_html("Intro", _SUB_PROSE, []),
    _OFFSITE_URL: _page_html("External", "off-site content that must never be ingested", []),
}


def _fake_get(url: str, *a: object, **k: object) -> httpx.Response:
    """``httpx.get`` replacement serving the in-memory link graph.

    An unknown URL 404s (the crawler skips it) — keeps the fixture honest
    if the crawl ever wandered somewhere unexpected.
    """
    body = _SITE.get(url, "")
    return httpx.Response(
        status_code=200 if body else 404,
        text=body,
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", url),
    )


async def _fake_embed(
    texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
) -> list[list[float]]:
    """Deterministic embedding stub — no provider traffic.

    A fixed vector so every seeded chunk is retrievable by the probe's
    query embedding (the F8 pre-retrieval returns a non-empty set).
    """
    return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


@contextlib.contextmanager
def _stub_embeds() -> Any:
    """Patch BOTH embed call-sites for the chain.

    Ingest (``movate.kb.ingest.embed_texts``) embeds the crawled pages;
    the F8 probe's pre-retrieval (``movate.kb.search.embed_texts``) embeds
    the probe query. Both must be stubbed for a hermetic, offline run.
    """
    with (
        mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed),
        mock.patch("movate.kb.search.embed_texts", side_effect=_fake_embed),
    ):
        yield


def _rag_generation_result(name: str) -> GenerationResult:
    """A valid RAG-shaped ``GenerationResult`` (reuses the mock payload).

    Lets the test drive a real (non ``--mock``) scaffold without a live
    LLM: the CLI writes a genuine canonical RAG agent and then proceeds
    through auto-ingest + grounded verify.
    """
    from movate.core.models import TokenUsage  # noqa: PLC0415
    from movate.providers.mock import _build_scaffold_response  # noqa: PLC0415

    payload = json.loads(_build_scaffold_response(name, grounding=True))
    return GenerationResult(agent=GeneratedAgent.model_validate(payload), tokens=TokenUsage())


def _patch_scaffolder(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Patch the LLM scaffolder to a deterministic RAG result (no model call)."""

    async def _fake_generate(**kwargs: object) -> GenerationResult:
        return _rag_generation_result(name)

    monkeypatch.setattr("movate.scaffold.generate_agent_from_description", _fake_generate)


# A grounded RAG answer matching the F3 grounded output schema. The F8
# verify probe's controlled provider returns this body.
_GROUNDED_ANSWER = json.dumps(
    {
        "answer": "Movate builds enterprise AI agents.",
        "citations": [1],
        "grounded": True,
        "confidence": 0.9,
    }
)


def _patch_verify_provider(monkeypatch: pytest.MonkeyPatch, *, response: str) -> None:
    """Patch the F8 verify's ``build_local_runtime`` to a controlled provider.

    The genuine ``Executor.execute`` path still runs (ADR-023 pre-retrieval
    against the seeded KB, input/output schema validation) — only the model
    response is swapped, so the probe is hermetic but real. The scaffold +
    auto-ingest earlier in init use their own runtime/storage, untouched.

    Mirrors ``test_init_llm_grounded_verify.py``'s helper.
    """
    import movate.cli._runtime as runtime_mod  # noqa: PLC0415
    from movate.core.executor import Executor  # noqa: PLC0415
    from movate.providers.mock import MockProvider  # noqa: PLC0415
    from movate.providers.pricing import load_pricing  # noqa: PLC0415
    from movate.storage import build_storage  # noqa: PLC0415
    from movate.testing import NullTracer  # noqa: PLC0415

    real_build = runtime_mod.build_local_runtime

    async def _fake_build(*, mock: bool) -> Any:
        if mock:
            return await real_build(mock=True)
        storage = build_storage()
        await storage.init()
        provider = MockProvider(response=response)
        executor = Executor(
            provider=provider,
            pricing=load_pricing(),
            storage=storage,
            tracer=NullTracer(),
            tenant_id="local",
        )

        @dataclass
        class _RT:
            executor: Any
            provider: Any
            storage: Any
            tracer: Any

        return _RT(executor=executor, provider=provider, storage=storage, tracer=NullTracer())

    monkeypatch.setattr(runtime_mod, "build_local_runtime", _fake_build)


def _read_chunks(agent: str) -> list[Any]:
    """Read every KB chunk for ``agent`` from the configured backend."""

    async def _go() -> list[Any]:
        from movate.storage import build_storage  # noqa: PLC0415

        storage = build_storage()
        await storage.init()
        try:
            return await storage.list_kb_chunks(agent=agent, tenant_id="local")
        finally:
            await storage.close()

    return asyncio.run(_go())


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``HOME`` at a tmp dir so nothing touches the developer's ~/.movate."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route storage to a tmp sqlite file + provide a stub embedding key.

    ``MOVATE_DB`` overrides ``~/.movate/local.db`` so the CLI never writes
    to the developer's home dir. ``OPENAI_API_KEY`` is the (stub) key the
    auto-ingest pre-flight gates on — embedding traffic is patched out.
    """
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "kb.db"))
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")


# ---------------------------------------------------------------------------
# The acceptance guard — the WHOLE chain in one `mdk init`.
# ---------------------------------------------------------------------------


@pytest.mark.acceptance
@pytest.mark.unit
class TestLlmUrlRagSliceAcceptance:
    """One hermetic ``mdk init`` exercising F3→F6→F7→F8 + the canonical
    layout. A regression in any stage fails one of the asserts below."""

    def test_url_to_rag_slice_end_to_end(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        name = "site-bot"
        monkeypatch.chdir(tmp_path)
        # F5/F6: serve the same-site link graph (no real network).
        monkeypatch.setattr(httpx, "get", _fake_get)
        # F3: deterministic RAG scaffold (no live LLM).
        _patch_scaffolder(monkeypatch, name)
        # F8: controlled grounded probe through the real Executor path.
        _patch_verify_provider(monkeypatch, response=_GROUNDED_ANSWER)

        with _stub_embeds():
            result = runner.invoke(
                app,
                [
                    "init",
                    name,
                    "--llm",
                    f"answer questions about {_START_URL}",
                    "--target",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, result.stdout + result.stderr
        agent_dir = tmp_path / name

        # -- Stage 1 (F3 + canonical layout #127): the agent is on disk in
        #    the ONE canonical layout and is a RAG shape. ----------------
        assert (agent_dir / "agent.yaml").is_file()
        assert (agent_dir / "prompt.md").is_file()
        assert (agent_dir / "schema" / "input.yaml").is_file()
        assert (agent_dir / "schema" / "output.yaml").is_file()
        assert (agent_dir / "evals" / "dataset.jsonl").is_file()
        assert (agent_dir / "evals" / "judge.yaml.example").is_file()

        spec = yaml.safe_load((agent_dir / "agent.yaml").read_text())
        # RAG markers: the kb skill + the ADR-023 auto-retrieval block.
        assert spec["skills"] == ["kb-vector-lookup"]
        assert spec["retrieval"]["auto_into"] == "context"
        # agent.yaml references the canonical YAML schema files (#127).
        assert spec["schema"]["input"] == "./schema/input.yaml"
        assert spec["schema"]["output"] == "./schema/output.yaml"

        # Input schema: `context` is OPTIONAL (auto-filled by pre-retrieval).
        input_schema = yaml.safe_load((agent_dir / "schema" / "input.yaml").read_text())
        assert "question" in input_schema["properties"]
        assert "context" in input_schema["properties"]
        assert "context" not in input_schema["required"]

        # The built-in skill was provisioned into the project skills/ dir.
        assert (tmp_path / "skills" / "kb-vector-lookup").is_dir()

        # -- Stage 2 (load/validate): the scaffold loads cleanly — skill
        #    resolution + the ADR-023 retrieval cross-link both resolve. --
        bundle = load_agent(agent_dir)
        assert bundle.spec.retrieval.auto_retrieval_enabled is True
        assert bundle.spec.retrieval.auto_into == "context"
        assert {s.spec.name for s in bundle.skills} == {"kb-vector-lookup"}
        validate_result = runner.invoke(app, ["validate", str(agent_dir)])
        assert validate_result.exit_code == 0, validate_result.stdout + validate_result.stderr

        # -- Stage 3 (F5/F6/F7): the URL was crawled (BFS followed a
        #    same-site link) and the crawl auto-ingested into the KB. ----
        chunks = _read_chunks(name)
        assert chunks, "expected chunks auto-ingested from the crawled URL"
        sources = {c.source for c in chunks}
        # Every chunk's source is a page the crawler actually fetched.
        assert sources <= {_START_URL, _SUB_URL}, sources
        # The seed page produced chunks (F5 single-page fetch path).
        assert _START_URL in sources
        # The crawler FOLLOWED the same-site link to the sub-page (F6 BFS) —
        # this is what distinguishes a real crawl from a single fetch.
        assert _SUB_URL in sources, "crawl did not follow the same-site link (F6 regression)"
        # The off-site link was NEVER followed (same-site guard).
        assert not any("external.example" in s for s in sources)
        # The ingested text actually came from the crawled pages.
        joined = "\n".join(c.text for c in chunks)
        assert "enterprise AI agents" in joined
        # The auto-ingest confirmation names the URL + a positive page count.
        assert "grounded on" in result.stdout
        assert _START_URL in result.stdout

        # -- Stage 4 (F8): the grounded verify probe ran THROUGH the agent
        #    (real Executor + ADR-023 pre-retrieval against the seeded KB)
        #    and reported success grounded on a positive retrieved count. -
        assert "Verifying grounded answer" in result.stderr
        assert "verified" in result.stdout
        match = re.search(r"grounded from\s+(\d+)\s+retrieved", result.stdout)
        assert match, result.stdout
        # >= 1 retrieved chunk proves the seeded KB was queried by the
        # probe's pre-retrieval phase — the end-to-end RAG actually works.
        assert int(match.group(1)) >= 1
