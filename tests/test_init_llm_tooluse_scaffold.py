"""F1' (#137): tool-use-intent detection → skill-stub scaffold in `mdk init --llm`.

Symmetric to F3 (grounding → RAG): when the ``--llm`` description implies
the agent must TAKE AN ACTION through a tool ("create a ticket", "look up an
order", "send a Slack message", "query the CRM", "book a meeting"), the
scaffolder emits a TOOL-USE-shaped agent wired to a SKILL STUB:

* ``agent.yaml`` declares ``skills: [<verb-derived-name>]`` (a NON-built-in
  skill the LLM/heuristic named), and NO ``retrieval`` block.
* a skill STUB is provisioned at ``skills/<name>/`` (``skill.yaml`` + an
  ``impl.py`` TODO handler) so the agent loads + validates + ``run --mock``
  works and the operator has a runnable starting point to fill in.
* the input/output is the conversational ``{request} → {answer, confidence}``
  shape — the action happens inside the tool call.

A pure Q&A / classifier / summarizer / extraction / RAG description scaffolds
exactly as before — NO skill stub (additive, no false positives). These tests
are hermetic: they run entirely through the offline ``--mock`` provider (no
API key, no network), which classifies tool-use intent deterministically the
same way the meta-prompt asks the real LLM to.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.loader import load_agent
from movate.providers.mock import (
    _build_scaffold_response,
    _derive_skill_name,
    _looks_like_grounding_description,
    _looks_like_tool_use_description,
)
from movate.scaffold import GeneratedAgent
from movate.scaffold.llm_scaffold import _EXAMPLE_TOOL_USE

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _scaffold_bare(
    *, name: str, description: str, target: Path, monkeypatch: pytest.MonkeyPatch
) -> object:
    """Run `mdk init <name> --llm <desc> --mock --bare --target <target>`.

    --bare keeps the standalone single-dir layout: the agent lands at
    ``target/<name>/`` and skills sit beside it at ``target/skills/``.
    """
    monkeypatch.chdir(target)
    return runner.invoke(
        app,
        ["init", name, "--llm", description, "--mock", "--bare", "--target", str(target)],
    )


def _scaffold_project(
    *, name: str, description: str, target: Path, monkeypatch: pytest.MonkeyPatch
) -> object:
    """Run the DEFAULT (project-mode, ADR 026) `mdk init <name> --llm ...`.

    Outside a project + not --bare → bootstrap a project at ``target/<name>/``
    with the agent under ``agents/<name>/`` and skills under the project
    ``skills/`` dir.
    """
    monkeypatch.chdir(target)
    return runner.invoke(
        app,
        ["init", name, "--llm", description, "--mock", "--target", str(target)],
    )


# ---------------------------------------------------------------------------
# Unit: tool-use-intent detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestToolUseDetection:
    @pytest.mark.parametrize(
        "description",
        [
            "create a ticket for a customer issue",
            "open a Jira issue from a bug report",
            "look up an order in our system",
            "send a Slack message to the on-call engineer",
            "post a message to the team channel",
            "query the CRM for a contact record",
            "book a meeting on the shared calendar",
            "schedule an appointment in the calendar",
            "update the record in Salesforce",
            "trigger a webhook on a new signup",
        ],
    )
    def test_tool_use_descriptions_detected(self, description: str) -> None:
        assert _looks_like_tool_use_description(description) is True

    @pytest.mark.parametrize(
        "description",
        [
            "answer general trivia questions",
            "classify short text into sentiment labels",
            "summarize a block of text into N words",
            "extract structured fields from an invoice",
            "translate English to French",
            "a helpful assistant that responds to user questions",
        ],
    )
    def test_non_tool_use_descriptions_not_detected(self, description: str) -> None:
        assert _looks_like_tool_use_description(description) is False

    def test_pure_qa_is_not_tool_use(self) -> None:
        """A pure Q&A description (verb but no external-system cue) is not
        tool-use — the conservative both-required rule guards it."""
        assert _looks_like_tool_use_description("answer questions about pricing") is False

    def test_verb_without_system_cue_is_not_tool_use(self) -> None:
        """An action verb with NO external-system object is not tool-use:
        "create a poem" transforms text, it doesn't call a tool."""
        assert _looks_like_tool_use_description("create a poem about the ocean") is False

    def test_system_cue_without_verb_is_not_tool_use(self) -> None:
        """A system noun with no action verb (e.g. classifying tickets) is
        not tool-use — it needs a verb that DOES something to the system."""
        assert _looks_like_tool_use_description("a chatbot that knows about tickets") is False

    def test_grounding_and_tool_use_are_disjoint_on_typical_descriptions(self) -> None:
        """A grounding description ("answer questions about our docs") is NOT
        tool-use, and a tool-use description ("create a ticket") is NOT
        grounding — the two intents don't collide on the common phrasings."""
        grounding = "answer questions about our help docs"
        assert _looks_like_grounding_description(grounding) is True
        assert _looks_like_tool_use_description(grounding) is False

        tool_use = "create a ticket for the issue"
        assert _looks_like_tool_use_description(tool_use) is True
        assert _looks_like_grounding_description(tool_use) is False


@pytest.mark.unit
class TestSkillNameDerivation:
    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            ("create a ticket for a customer issue", "create-ticket"),
            ("look up an order in our system", "look-order"),
            ("send a Slack message to the team", "send-slack"),
            ("query the CRM for a contact", "query-crm"),
        ],
    )
    def test_derive_skill_name(self, description: str, expected: str) -> None:
        assert _derive_skill_name(description) == expected

    def test_derived_name_is_a_valid_skill_slug(self) -> None:
        """Every derived name obeys the skill-name rule (lowercase, leading
        letter, hyphen-separated) so it scaffolds + loads without coercion."""
        for desc in (
            "create a ticket",
            "send an email",
            "book a meeting",
            "update the database record",
        ):
            slug = _derive_skill_name(desc)
            assert re.fullmatch(r"[a-z][a-z0-9-]*", slug), slug

    def test_unsluggable_description_falls_back_to_default(self) -> None:
        """A description with no verb/system match returns the generic
        default rather than an empty / invalid name."""
        assert _derive_skill_name("???") == "external-action"


# ---------------------------------------------------------------------------
# Unit: synthesized tool-use scaffold shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSynthesizedToolUseShape:
    def test_tool_use_exemplar_is_valid_and_tool_shaped(self) -> None:
        payload = json.loads(_EXAMPLE_TOOL_USE)
        ay = payload["agent_yaml"]
        assert ay["skills"] == ["create-ticket"]
        # NO retrieval block — that key is grounding-only.
        assert "retrieval" not in ay
        assert set(payload["output_schema"]["required"]) == {"answer", "confidence"}
        assert "request" in payload["input_schema"]["properties"]

    def test_mock_tool_use_payload_is_tool_shaped(self) -> None:
        payload = json.loads(_build_scaffold_response("ticket-bot", tool_use_skill="create-ticket"))
        ay = payload["agent_yaml"]
        assert ay["skills"] == ["create-ticket"]
        assert "retrieval" not in ay
        assert "request" in payload["input_schema"]["properties"]
        assert set(payload["output_schema"]["required"]) == {"answer", "confidence"}
        # The prompt steers the model to USE the tool.
        assert "tool" in payload["prompt_md"].lower()

    def test_mock_tool_use_payload_validates_as_generated_agent(self) -> None:
        payload = json.loads(_build_scaffold_response("any-name", tool_use_skill="do-thing"))
        agent = GeneratedAgent.model_validate(payload)
        assert agent.agent_yaml["skills"] == ["do-thing"]

    def test_grounding_wins_over_tool_use_in_builder(self) -> None:
        """``grounding=True`` short-circuits before the tool-use branch — a
        description can't be both, and grounding is checked first."""
        payload = json.loads(
            _build_scaffold_response("x", grounding=True, tool_use_skill="create-ticket")
        )
        assert payload["agent_yaml"]["skills"] == ["kb-vector-lookup"]
        assert "retrieval" in payload["agent_yaml"]

    def test_no_tool_use_skill_falls_back_to_qa(self) -> None:
        """Passing neither grounding nor tool_use_skill yields the QA shape —
        back-compat default unchanged."""
        default = json.loads(_build_scaffold_response("x"))
        assert "skills" not in default["agent_yaml"]
        assert set(default["output_schema"]["required"]) == {"answer", "confidence"}


# ---------------------------------------------------------------------------
# CLI end-to-end — tool-use scaffold (the F1' happy path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestToolUseScaffoldEndToEnd:
    def test_tool_use_description_scaffolds_skill_stub_bare(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _scaffold_bare(
            name="ticket-bot",
            description="create a ticket for a customer issue",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        agent_yaml = tmp_path / "ticket-bot" / "agent.yaml"
        assert agent_yaml.is_file()
        spec = yaml.safe_load(agent_yaml.read_text())

        # Tool-use shape: a NON-built-in skill, no retrieval block.
        assert spec["skills"] == ["create-ticket"]
        assert "retrieval" not in spec

        # The skill STUB was provisioned BESIDE the agent (--bare layout).
        skill_dir = tmp_path / "skills" / "create-ticket"
        assert skill_dir.is_dir()
        assert (skill_dir / "skill.yaml").is_file()
        # A handler stub exists (impl.py is the default echo-skill handler).
        assert (skill_dir / "impl.py").is_file()

        # The success output points the operator at the stub + the TODO.
        assert "create-ticket" in result.stdout
        assert "TODO" in result.stdout

    def test_tool_use_scaffold_passes_load_agent(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The written tool-use scaffold loads cleanly — the declared skill
        stub resolves against the project skills/ registry."""
        result = _scaffold_bare(
            name="order-bot",
            description="look up an order in our system",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        bundle = load_agent(tmp_path / "order-bot")
        assert {s.spec.name for s in bundle.skills} == {"look-order"}
        # NOT a RAG agent — pre-retrieval stays OFF.
        assert bundle.spec.retrieval.auto_retrieval_enabled is False

    def test_tool_use_scaffold_passes_mdk_validate(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init_result = _scaffold_bare(
            name="slack-bot",
            description="send a Slack message to the on-call engineer",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert init_result.exit_code == 0, init_result.stdout + init_result.stderr
        validate_result = runner.invoke(app, ["validate", str(tmp_path / "slack-bot")])
        assert validate_result.exit_code == 0, validate_result.stdout + validate_result.stderr

    def test_tool_use_scaffold_runs_mock(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk run --mock` works against the scaffolded tool-use agent — the
        agent + its skill stub form a runnable bundle offline."""
        init_result = _scaffold_bare(
            name="crm-bot",
            description="query the CRM for a contact record",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert init_result.exit_code == 0, init_result.stdout + init_result.stderr
        run_result = runner.invoke(
            app,
            [
                "run",
                str(tmp_path / "crm-bot"),
                "--mock",
                json.dumps({"request": "find the contact for Acme Corp"}),
            ],
        )
        assert run_result.exit_code == 0, run_result.stdout + run_result.stderr
        result_json = json.loads(run_result.stdout)
        assert result_json["status"] == "success"

    def test_tool_use_skill_stub_is_directly_runnable(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scaffolded stub runs via `mdk skills run` (the default echo
        handler) — a true starting point, not a broken placeholder."""
        init_result = _scaffold_bare(
            name="ticket-bot",
            description="create a ticket for a customer issue",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert init_result.exit_code == 0, init_result.stdout + init_result.stderr
        monkeypatch.chdir(tmp_path)
        run_result = runner.invoke(
            app,
            ["skills", "run", "create-ticket", json.dumps({"query": "broken login"})],
        )
        assert run_result.exit_code == 0, run_result.stdout + run_result.stderr


@pytest.mark.unit
class TestToolUseScaffoldProjectMode:
    def test_project_mode_puts_stub_in_project_skills_dir(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR 026 default (project mode): the agent lands under
        ``agents/<name>/`` and the skill stub under the project ``skills/``
        dir (NOT beside the agent)."""
        result = _scaffold_project(
            name="ticket-bot",
            description="create a ticket for a customer issue",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        project_root = tmp_path / "ticket-bot"
        # Project wrapper exists with the agent under agents/.
        assert (project_root / "project.yaml").is_file()
        agent_dir = project_root / "agents" / "ticket-bot"
        assert (agent_dir / "agent.yaml").is_file()

        # The skill stub is in the PROJECT skills/ dir, not under the agent.
        project_skill = project_root / "skills" / "create-ticket"
        assert project_skill.is_dir()
        assert (project_skill / "skill.yaml").is_file()
        assert (project_skill / "impl.py").is_file()
        # NOT scaffolded beside the agent.
        assert not (agent_dir / "skills" / "create-ticket").exists()

        spec = yaml.safe_load((agent_dir / "agent.yaml").read_text())
        assert spec["skills"] == ["create-ticket"]

    def test_project_mode_tool_use_agent_loads(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _scaffold_project(
            name="order-bot",
            description="look up an order in our system",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        agent_dir = tmp_path / "order-bot" / "agents" / "order-bot"
        bundle = load_agent(agent_dir)
        assert {s.spec.name for s in bundle.skills} == {"look-order"}


# ---------------------------------------------------------------------------
# CLI end-to-end — regression guards (no false-positive stubs)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNonToolUseRegression:
    @pytest.mark.parametrize(
        ("name", "description"),
        [
            ("qa", "answer general trivia questions"),
            ("sentiment", "classify short text into sentiment labels"),
            ("tldr", "summarize a block of text into a short paragraph"),
            ("xform", "extract structured fields from an invoice"),
        ],
    )
    def test_non_tool_use_scaffold_has_no_skill_stub(
        self,
        name: str,
        description: str,
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = _scaffold_bare(
            name=name, description=description, target=tmp_path, monkeypatch=monkeypatch
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        spec = yaml.safe_load((tmp_path / name / "agent.yaml").read_text())
        assert "skills" not in spec
        assert "retrieval" not in spec
        # No skills/ dir is created for a non-tool-use scaffold.
        assert not (tmp_path / "skills").exists()

    def test_rag_description_still_scaffolds_rag_not_tool_use(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A grounding description still scaffolds the RAG shape (unchanged
        F3 behavior) — it is NOT mistaken for tool-use."""
        result = _scaffold_bare(
            name="docs-qa",
            description="answer questions about our help docs",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        spec = yaml.safe_load((tmp_path / "docs-qa" / "agent.yaml").read_text())
        # RAG shape: built-in retrieval skill + retrieval block.
        assert spec["skills"] == ["kb-vector-lookup"]
        assert spec["retrieval"]["auto_into"] == "context"
        # The provisioned skill is the BUILT-IN retrieval skill, not a stub.
        assert (tmp_path / "skills" / "kb-vector-lookup").is_dir()
        assert not (tmp_path / "skills" / "create-ticket").exists()

    def test_create_summary_is_summarizer_not_tool_use(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transformation shape wins over tool-use: "create a summary of a
        meeting transcript" is a summarizer (no skill stub), even though it
        has an action verb + a system-ish noun ("meeting")."""
        result = _scaffold_bare(
            name="recap",
            description="create a summary of a meeting transcript",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        spec = yaml.safe_load((tmp_path / "recap" / "agent.yaml").read_text())
        assert "skills" not in spec
        assert set(spec_keys_output(tmp_path / "recap")) == {"summary", "key_points"}
        assert not (tmp_path / "skills").exists()


def spec_keys_output(agent_dir: Path) -> list[str]:
    """The required keys of the agent's output schema (canonical YAML layout)."""
    out = yaml.safe_load((agent_dir / "schema" / "output.yaml").read_text())
    return list(out["required"])
