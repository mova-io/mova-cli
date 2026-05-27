"""F8 (#117) — ``mdk init --llm`` grounded end-to-end verify.

The final loop-closer: after ``mdk init bot "answer questions about
https://movate.com" --llm`` scaffolds a RAG agent (F3) AND auto-ingest
populates its KB (F7), run ONE grounded probe query THROUGH the agent
(reusing the existing local-run / Executor path with ADR-023
auto-retrieval) and confirm it answers FROM the KB — immediate proof the
end-to-end RAG works.

Behavior under test (all hermetic — MockProvider, stubbed network + embed,
tmp sqlite via ``MOVATE_DB``; no API keys, no real traffic):

* RAG scaffold + populated KB + a (mocked) grounded run → ``✓ verified``.
* Ungrounded run result → soft warning, exit 0.
* No key / empty KB / ingest skipped → verify skipped + manual hint, exit 0.
* ``--mock`` → structural smoke only (runs, no real-grounding assertion).
* ``--no-verify`` → verify not attempted.
* Execution error during the probe → warn + skip, exit 0 (scaffold intact).

The grounding outcome is controlled by patching ``build_local_runtime`` to
return a runtime whose provider emits a chosen JSON body — so the genuine
``Executor.execute`` path (input-schema validation, ADR-023 pre-retrieval,
output-schema validation) runs end-to-end against a seeded KB without a live
model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.scaffold import GeneratedAgent, GenerationResult

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures / fixture data (mirrors test_init_llm_auto_ingest.py conventions)
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

# A grounded RAG answer (matches the F3 grounded output schema).
_GROUNDED_ANSWER = json.dumps(
    {
        "answer": "Movate builds enterprise AI agents.",
        "citations": [1],
        "grounded": True,
        "confidence": 0.9,
    }
)
# A successful-but-ungrounded RAG answer (grounded=false, no citations).
_UNGROUNDED_ANSWER = json.dumps(
    {
        "answer": "The context does not cover that.",
        "citations": [],
        "grounded": False,
        "confidence": 0.0,
    }
)


def _fake_response(text: str, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        text=text,
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", "https://movate.com"),
    )


async def _fake_embed(
    texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
) -> list[list[float]]:
    """Deterministic stub — no embedding-provider traffic. Fixed vector so
    the seeded KB chunks are retrievable by the probe's query embedding."""
    return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def _stub_embeds() -> Any:
    """Patch BOTH embed call-sites for the duration of a `with` block.

    Ingest (``movate.kb.ingest.embed_texts``) embeds the crawled pages;
    the F8 probe's pre-retrieval (``movate.kb.search.embed_texts``) embeds
    the probe query. Both must be stubbed for a hermetic, offline run.
    Returns a context manager combining the two patches.
    """
    import contextlib  # noqa: PLC0415

    @contextlib.contextmanager
    def _ctx() -> Any:
        with (
            mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed),
            mock.patch("movate.kb.search.embed_texts", side_effect=_fake_embed),
        ):
            yield

    return _ctx()


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
    """A valid RAG-shaped GenerationResult, reusing the mock's payload."""
    from movate.core.models import TokenUsage  # noqa: PLC0415
    from movate.providers.mock import _build_scaffold_response  # noqa: PLC0415

    payload = json.loads(_build_scaffold_response(name, grounding=True))
    return GenerationResult(agent=GeneratedAgent.model_validate(payload), tokens=TokenUsage())


def _patch_scaffolder(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Patch the scaffolder to return a deterministic RAG result (no LLM)."""

    async def _fake_generate(**kwargs: object) -> GenerationResult:
        return _rag_generation_result(name)

    monkeypatch.setattr("movate.scaffold.generate_agent_from_description", _fake_generate)


def _patch_verify_provider(
    monkeypatch: pytest.MonkeyPatch, *, response: str | None, raise_on_execute: bool = False
) -> None:
    """Patch ``build_local_runtime`` so the F8 verify probe runs against a
    controlled provider instead of a live model.

    The genuine ``Executor.execute`` path still runs (ADR-023 pre-retrieval
    against the seeded KB, input/output schema validation) — only the model
    response is swapped. When ``raise_on_execute`` is set, the executor's
    ``execute`` raises so the verify's catch-all skip path is exercised.

    Importantly: only the F8 verify's ``build_local_runtime`` (imported
    inside ``_run_grounded_probe``) is patched — the scaffold + auto-ingest
    earlier in init use their own runtime/storage and are left untouched.
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
            # --mock smoke: defer to the real builder (deterministic mock).
            return await real_build(mock=True)
        storage = build_storage()
        await storage.init()
        provider = MockProvider(response=response or "{}")
        executor = Executor(
            provider=provider,
            pricing=load_pricing(),
            storage=storage,
            tracer=NullTracer(),
            tenant_id="local",
        )
        if raise_on_execute:

            async def _boom(*a: object, **k: object) -> Any:
                raise RuntimeError("probe execution exploded")

            executor.execute = _boom  # type: ignore[method-assign]

        @dataclass
        class _RT:
            executor: Any
            provider: Any
            storage: Any
            tracer: Any

        return _RT(executor=executor, provider=provider, storage=storage, tracer=NullTracer())

    monkeypatch.setattr(runtime_mod, "build_local_runtime", _fake_build)


def _ban_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if the F8 verify ever runs a probe.

    Patches the probe driver (NOT ``build_local_runtime``, which the
    scaffold + ingest legitimately use) so the "verify is skipped" paths
    can prove no probe was attempted without breaking the rest of init.
    """

    async def _boom(**kwargs: object) -> None:
        raise AssertionError("a verify probe must not run on this path")

    monkeypatch.setattr("movate.cli.init._run_grounded_probe", _boom)


def _read_chunks(agent: str) -> list[object]:
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


def _invoke_init(tmp_path: Path, name: str, url: str, *extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "init",
            name,
            "--llm",
            f"answer questions about {url}",
            "--target",
            str(tmp_path),
            *extra,
        ],
    )


# ---------------------------------------------------------------------------
# Happy path — grounded run reports success
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGroundedVerifyHappyPath:
    def test_populated_kb_grounded_run_reports_verified(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RAG scaffold + populated KB + a grounded probe run → ``✓ verified``
        with the retrieved-chunk count, exit 0."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response(_PAGE_HTML))
        _patch_scaffolder(monkeypatch, "verify-bot")
        _patch_verify_provider(monkeypatch, response=_GROUNDED_ANSWER)
        url = "https://movate.com"

        with _stub_embeds():
            result = _invoke_init(tmp_path, "verify-bot", url)

        assert result.exit_code == 0, result.stdout + result.stderr
        # Scaffold + KB intact.
        spec = yaml.safe_load((tmp_path / "verify-bot" / "agent.yaml").read_text())
        assert spec["retrieval"]["auto_into"] == "context"
        assert _read_chunks("verify-bot"), "expected auto-ingested chunks"
        # The announce line + the verified success line, naming a positive
        # retrieved-chunk count (proves the seeded KB was actually queried by
        # the ADR-023 pre-retrieval phase of the probe run).
        import re  # noqa: PLC0415

        assert "Verifying grounded answer" in result.stderr
        assert "verified" in result.stdout
        match = re.search(r"grounded from\s+(\d+)\s+retrieved", result.stdout)
        assert match, result.stdout
        assert int(match.group(1)) >= 1

    def test_ungrounded_run_is_soft_warning(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A successful-but-ungrounded answer (grounded=false, no citations)
        → soft warning, NOT a hard fail; exit 0, scaffold + KB intact."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response(_PAGE_HTML))
        _patch_scaffolder(monkeypatch, "ungrounded-bot")
        _patch_verify_provider(monkeypatch, response=_UNGROUNDED_ANSWER)
        url = "https://movate.com"

        with _stub_embeds():
            result = _invoke_init(tmp_path, "ungrounded-bot", url)

        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "ungrounded-bot" / "agent.yaml").is_file()
        assert _read_chunks("ungrounded-bot")
        # Soft warning — NOT a ✓ verified line.
        assert "wasn't" in result.stderr or "not" in result.stderr.lower()
        assert "verified:" not in result.stdout


# ---------------------------------------------------------------------------
# Best-effort — never breaks init
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGroundedVerifyNeverBreaksInit:
    def test_empty_kb_skips_verify_with_hint(
        self,
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No embedding key → auto-ingest skipped → KB empty → verify skipped
        with the manual hint, exit 0. (No verify probe is attempted.)"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MOVATE_DB", str(tmp_path / "kb.db"))
        monkeypatch.delenv("MOVATE_DB_URL", raising=False)
        # Non-OpenAI provider key clears the scaffold pre-flight; the missing
        # OPENAI_API_KEY is what gates auto-ingest (→ empty KB → no verify).
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")
        _patch_scaffolder(monkeypatch, "nokey-bot")
        _ban_probe(monkeypatch)
        url = "https://movate.com"

        result = _invoke_init(tmp_path, "nokey-bot", url)

        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "nokey-bot" / "agent.yaml").is_file()
        assert _read_chunks("nokey-bot") == []
        # Verify skipped for empty KB + manual hint surfaced.
        assert "KB is empty" in result.stderr
        assert "mdk run" in result.stderr

    def test_execution_error_warns_and_skips(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An exception during the probe run → warn + skip, exit 0, scaffold +
        KB intact (the verify never changes a successful scaffold's exit)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response(_PAGE_HTML))
        _patch_scaffolder(monkeypatch, "boom-bot")
        _patch_verify_provider(monkeypatch, response=_GROUNDED_ANSWER, raise_on_execute=True)
        url = "https://movate.com"

        with _stub_embeds():
            result = _invoke_init(tmp_path, "boom-bot", url)

        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "boom-bot" / "agent.yaml").is_file()
        assert _read_chunks("boom-bot")
        assert "grounded verify skipped" in result.stderr
        assert "verified:" not in result.stdout


# ---------------------------------------------------------------------------
# --mock structural smoke + --no-verify opt-out + non-grounding
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGroundedVerifyModes:
    def test_mock_runs_structural_smoke_only(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--mock → no network, no real grounding asserted; the agent RUNS
        against the mock provider (structural smoke), exit 0."""
        monkeypatch.chdir(tmp_path)

        def _should_not_fetch(*a: object, **k: object) -> httpx.Response:
            raise AssertionError("--mock must stay offline")

        monkeypatch.setattr(httpx, "get", _should_not_fetch)
        url = "https://movate.com"

        # The KB is empty under --mock, but the kb-vector-lookup skill still
        # embeds the probe query. Stub the embedder so the smoke stays
        # offline (the search returns [] against the empty KB; pre-retrieval
        # no-ops and the run proceeds — exactly the structural smoke we want).
        with mock.patch("movate.kb.search.embed_texts", side_effect=_fake_embed):
            result = _invoke_init(tmp_path, "smoke-bot", url, "--mock")

        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "smoke-bot" / "agent.yaml").is_file()
        # KB is empty under --mock (no ingest), but the verify STILL runs a
        # structural smoke (the mock-mode branch doesn't require chunks).
        assert _read_chunks("smoke-bot") == []
        assert "Verifying grounded answer" in result.stderr
        assert "smoke" in result.stdout
        # Mock mode must NOT claim a real grounded verification.
        assert "verified:" not in result.stdout

    def test_no_verify_skips_probe(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--no-verify → the probe is never attempted (no build_local_runtime
        call for verify), exit 0. Ingest still runs (independent flag)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response(_PAGE_HTML))
        _patch_scaffolder(monkeypatch, "skip-bot")
        _ban_probe(monkeypatch)
        url = "https://movate.com"

        with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
            result = _invoke_init(tmp_path, "skip-bot", url, "--no-verify")

        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "skip-bot" / "agent.yaml").is_file()
        # Ingest still happened (--no-verify is independent of --no-ingest).
        assert _read_chunks("skip-bot")
        assert "--no-verify" in result.stderr
        # No probe was announced/run.
        assert "Verifying grounded answer" not in result.stderr

    def test_no_ingest_leaves_nothing_to_verify(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--no-ingest (KB empty) → verify has nothing to do → skipped with
        the manual hint, exit 0, no probe attempted."""
        monkeypatch.chdir(tmp_path)
        _patch_scaffolder(monkeypatch, "noingest-bot")

        def _should_not_fetch(*a: object, **k: object) -> httpx.Response:
            raise AssertionError("--no-ingest must not hit the network")

        monkeypatch.setattr(httpx, "get", _should_not_fetch)
        _ban_probe(monkeypatch)
        url = "https://movate.com"

        result = _invoke_init(tmp_path, "noingest-bot", url, "--no-ingest")

        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "noingest-bot" / "agent.yaml").is_file()
        assert _read_chunks("noingest-bot") == []
        assert "KB is empty" in result.stderr

    def test_non_grounding_scaffold_no_verify(
        self,
        tmp_path: Path,
        isolated_home: Path,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-grounding scaffold → no RAG output contract → verify NOT
        attempted (and no probe announce line), exit 0."""
        monkeypatch.chdir(tmp_path)
        _ban_probe(monkeypatch)

        result = runner.invoke(
            app,
            [
                "init",
                "classifier-bot",
                "--llm",
                "classify short text into sentiment labels",
                "--mock",
                "--target",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        spec = yaml.safe_load((tmp_path / "classifier-bot" / "agent.yaml").read_text())
        assert "retrieval" not in spec
        assert "Verifying grounded answer" not in result.stderr
