"""Every packaged template must scaffold, validate, and run end-to-end with --mock."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from movate.core.eval import (
    EvalEngine,
    assert_cross_family,
    load_judge_config,
)
from movate.core.executor import Executor
from movate.core.loader import _load_schema_doc, load_agent
from movate.core.models import JudgeConfig, JudgeMethod, RunRequest
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.templates import (
    TEMPLATES,
    TEMPLATES_DIR,
    get_template_path,
    list_templates,
)
from movate.testing import (
    InMemoryStorage,
    JudgeStubProvider,
    NullTracer,
    scaffold_agent,
)

# Per-template canonical input + an output the MockProvider can return that
# satisfies that template's output schema. Keep these in sync with the
# template directories.
CANONICAL: dict[str, tuple[dict, str]] = {
    "default": (
        {"text": "hello"},
        '{"message": "ok"}',
    ),
    "faq": (
        {"question": "What is movate?"},
        '{"answer": "A platform for agents.", "confidence": 0.9}',
    ),
    "summarizer": (
        {"text": "One two three four five six seven eight.", "max_words": 5},
        '{"summary": "Eight words counted briefly here.", "word_count": 5}',
    ),
    "classifier": (
        {
            "text": "I loved this movie!",
            "labels": ["positive", "negative", "neutral"],
        },
        '{"label": "positive"}',
    ),
    "chatbot": (
        {"message": "hi there"},
        '{"reply": "Hi! How can I help?"}',
    ),
    "extractor": (
        {"text": "Sarah (sarah@acme.io) needs help — production is broken."},
        '{"contact_name": "Sarah", "email": "sarah@acme.io", '
        '"intent": "support_request", "urgency": "high"}',
    ),
    # --- Role-based templates ---
    "rag-qa": (
        {
            "question": "What is the refund window?",
            "context": ["Refunds are honored within 30 days of purchase."],
        },
        '{"answer": "30 days.", "citations": [1], "grounded": true, "confidence": 0.95}',
    ),
    "ticket-triager": (
        {"subject": "Login broken", "body": "Cannot log in since this morning."},
        '{"category": "bug", "priority": "p1_high", "routing_queue": "engineering", '
        '"draft_reply": "Sorry — engineering is investigating now.", '
        '"confidence": 0.9}',
    ),
    "email-responder": (
        {
            "from": "alice@acme.com",
            "subject": "Q2 renewal",
            "body": "Are we renewing in May?",
            "intent": "Confirm renewal date with account manager.",
            "tone": "professional",
            "length": "short",
        },
        '{"subject": "Re: Q2 renewal", "body": "Hi Alice — looping in your '
        'account manager today.", "needs_review": true, "flags": ["confirm AM"]}',
    ),
    "sql-writer": (
        {
            "question": "How many users signed up last week?",
            "schema": "users(id int, signed_up_at timestamp)",
            "dialect": "postgres",
        },
        '{"query": "SELECT COUNT(*) FROM users WHERE signed_up_at >= '
        "NOW() - INTERVAL '7 days'\", "
        '"explanation": "Counts users in the last 7 days.", '
        '"tables_used": ["users"], "read_only": true, "confidence": 0.95}',
    ),
    "code-reviewer": (
        {
            "diff": "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-pass\n+pass  # nit\n",
            "language": "python",
        },
        '{"summary": "Trivial change.", "verdict": "approve", "findings": []}',
    ),
    "lead-qualifier": (
        {
            "name": "Sarah Chen",
            "company": "Acme (2000 employees)",
            "title": "VP Eng",
            "source": "demo_request",
            "message": "Budget approved. Want a demo this week.",
        },
        '{"bant": {'
        '"budget": {"score": 3, "rationale": "Approved."}, '
        '"authority": {"score": 3, "rationale": "VP."}, '
        '"need": {"score": 3, "rationale": "Evaluating."}, '
        '"timeline": {"score": 3, "rationale": "This week."}'
        '}, "total_score": 12, "next_action": "book_meeting", '
        '"rationale": "Textbook qualified.", "objections": []}',
    ),
    "meeting-summarizer": (
        {
            "title": "Standup",
            "attendees": ["Alice", "Bob"],
            "transcript": "Alice: I'll ship the migration today. Bob: thanks.",
        },
        '{"tldr": "Alice will ship the migration today.", '
        '"decisions": [], '
        '"action_items": [{"task": "Ship migration", "owner": "Alice", '
        '"due": "today"}], '
        '"blockers": [], "follow_ups": []}',
    ),
    "resume-screener": (
        {
            "job_description": "Senior Python engineer. 5+ years Python.",
            "resume": "Jane Doe. 7 years Python. FastAPI + Postgres.",
        },
        '{"match_score": 85, "strengths": ["7 years Python"], '
        '"gaps": ["No FastAPI specifics shown"], '
        '"interview_questions": ["Tell us about your most complex Python project."], '
        '"recommendation": "advance", "rationale": "Strong match."}',
    ),
    "compliance-checker": (
        {
            "text": "Our product cures headaches in 5 minutes guaranteed.",
            "rules": [{"id": "R1", "description": "No absolute medical claims."}],
        },
        '{"compliant": false, "violations": [{'
        '"rule_id": "R1", "excerpt": "cures headaches", "severity": "high", '
        '"explanation": "Absolute medical claim.", '
        '"suggested_rewording": "may help relieve headache symptoms"'
        '}], "summary": "One violation — rewrite before publishing."}',
    ),
    "research-agent": (
        {
            "topic": "Is GPT-4o-mini production-ready?",
            "sources": [
                {
                    "title": "OAI",
                    "url": "https://example.com",
                    "content": "60% quality at 6% cost.",
                }
            ],
        },
        '{"executive_summary": "Production-ready for cost-sensitive workloads [1].", '
        '"key_points": [{"claim": "60% quality at 6% cost.", "citations": [1]}], '
        '"disagreements": [], "open_questions": []}',
    ),
    "hr-policy": (
        {
            "question": "How many PTO days do I get per year?",
            "context": [
                "Full-time employees accrue 15 days of paid time off per calendar year "
                "during their first two years of employment.",
            ],
        },
        '{"answer": "Full-time employees accrue 15 days of PTO per calendar year '
        'during their first two years of employment [1].", '
        '"citations": [1], "grounded": true, "confidence": 0.97, '
        '"needs_escalation": false}',
    ),
    # Skill-demo templates — MockProvider returns the output directly;
    # skill execution (calculator Python impl / HTTP lookup) is bypassed.
    "calc-agent": (
        {"expression": "2 + 3"},
        '{"result": 5.0, "explanation": "2 plus 3 equals 5."}',
    ),
    "lookup-agent": (
        {"user_id": 1, "question": "What is their email address?"},
        '{"answer": "Their email address is Sincere@april.biz.", "user_found": true}',
    ),
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_template_registry_exposes_all_known() -> None:
    assert set(TEMPLATES.keys()) == {
        # Original core templates
        "default",
        "faq",
        "summarizer",
        "classifier",
        "chatbot",
        "extractor",
        # Role-based templates (post-v1.0)
        "rag-qa",
        "ticket-triager",
        "email-responder",
        "sql-writer",
        "code-reviewer",
        "lead-qualifier",
        "meeting-summarizer",
        "resume-screener",
        "compliance-checker",
        "research-agent",
        "hr-policy",
        # Skill-demo templates (post-v1.1)
        "calc-agent",
        "lookup-agent",
    }
    assert list_templates() == sorted(TEMPLATES.keys())


@pytest.mark.unit
@pytest.mark.parametrize("name", list(TEMPLATES.keys()))
def test_template_dir_is_present_and_complete(name: str) -> None:
    """Every bundled template conforms to the canonical agent layout (#127).

    The canonical layout standardizes ALL bundled templates on one shape,
    so ``mdk init``, ``mdk add``, and ``mdk init --llm`` all produce
    identical directories::

        <name>/
          agent.yaml      # schema refs are FILE paths: ./schema/{input,output}.yaml
          prompt.md
          evals/
            dataset.jsonl
            judge.yaml.example
          schema/
            input.yaml    # canonical YAML (never inline, never .json)
            output.yaml

    The loader still accepts inline shorthand and ``schema/*.json`` for
    hand-authored user agents (back-compat); this assertion governs only
    what the SHIPPED templates look like. See ``docs/agent-layout.md``.
    """
    path = get_template_path(name)
    assert (path / "agent.yaml").is_file()
    assert (path / "prompt.md").is_file()
    assert (path / "evals" / "dataset.jsonl").is_file()

    # Schemas live in canonical YAML files, referenced by file path.
    assert (path / "schema" / "input.yaml").is_file(), f"{name}: missing schema/input.yaml"
    assert (path / "schema" / "output.yaml").is_file(), f"{name}: missing schema/output.yaml"
    # No leftover JSON schema files — templates standardized on YAML.
    assert not (path / "schema" / "input.json").exists(), f"{name}: schema/input.json not converted"
    assert not (path / "schema" / "output.json").exists(), (
        f"{name}: schema/output.json not converted"
    )

    spec = yaml.safe_load((path / "agent.yaml").read_text())
    schema_block = spec.get("schema", {})
    assert schema_block.get("input") == "./schema/input.yaml", (
        f"{name}: agent.yaml schema.input must reference ./schema/input.yaml "
        f"(got {schema_block.get('input')!r})"
    )
    assert schema_block.get("output") == "./schema/output.yaml", (
        f"{name}: agent.yaml schema.output must reference ./schema/output.yaml "
        f"(got {schema_block.get('output')!r})"
    )

    # Every template ships a judge.yaml.example (canonical layout). Its
    # `method` matches the template's eval nature — see the dedicated
    # tests below for the exact-match vs llm_judge split.
    assert (path / "evals" / "judge.yaml.example").is_file(), (
        f"{name}: missing evals/judge.yaml.example"
    )


@pytest.mark.unit
def test_template_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown template"):
        get_template_path("nope")


# ---------------------------------------------------------------------------
# Scaffold + load each template
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("template", list(TEMPLATES.keys()))
def test_scaffold_each_template_loads(template: str, tmp_path: Path) -> None:
    """Scaffolded directory must validate via the loader."""
    dst = tmp_path / template
    scaffold_agent(dst, name="demo", template=template)
    bundle = load_agent(dst)
    assert bundle.spec.api_version == "movate/v1"
    assert bundle.spec.kind == "Agent"
    assert bundle.spec.name == "demo"


@pytest.mark.unit
@pytest.mark.parametrize("template", list(TEMPLATES.keys()))
def test_template_dataset_is_well_formed_jsonl(template: str, tmp_path: Path) -> None:
    """Every dataset row parses and has both 'input' and 'expected' keys."""
    dst = tmp_path / template
    scaffold_agent(dst, name="demo", template=template)
    raw = (dst / "evals" / "dataset.jsonl").read_bytes().decode().splitlines()
    rows = [json.loads(line) for line in raw if line.strip()]
    assert len(rows) >= 1
    for row in rows:
        assert "input" in row
        assert "expected" in row


# ---------------------------------------------------------------------------
# End-to-end execution per template (mock provider, canonical input)
# ---------------------------------------------------------------------------


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def tracer() -> NullTracer:
    return NullTracer()


@pytest.mark.unit
@pytest.mark.parametrize("template", list(TEMPLATES.keys()))
async def test_template_runs_end_to_end_with_mock(
    template: str,
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    dst = tmp_path / template
    scaffold_agent(dst, name="demo", template=template)
    bundle = load_agent(dst)

    payload, mock_response = CANONICAL[template]
    provider = MockProvider(response=mock_response)
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)

    response = await executor.execute(bundle, RunRequest(agent="demo", input=payload))
    assert response.status == "success", f"{template} failed: {response.error}"
    # Output validates against template's schema
    assert response.data == json.loads(mock_response)


# ---------------------------------------------------------------------------
# judge.yaml.example presence + method matches the template's eval nature
# ---------------------------------------------------------------------------
#
# Canonical layout (#127): EVERY template ships an ``evals/judge.yaml.example``.
# The example's ``method`` is the honest scoring mode for that template:
#   * free-text output (prose answer, drafted reply, summary) → ``llm_judge``
#     (exact-match can never pass wording that varies run to run), and
#   * fixed-shape typed output (finite-label classifier, structured-field
#     extractor) → ``exact`` (a correct output string-equals the expected one,
#     no LLM judge / API cost needed).


def _load_judge_example_method(dst: Path) -> JudgeMethod:
    """Parse a scaffolded template's judge.yaml.example into its method."""
    doc = yaml.safe_load((dst / "evals" / "judge.yaml.example").read_text())
    return JudgeConfig(**doc).method


@pytest.mark.unit
@pytest.mark.parametrize("template", ["faq", "summarizer", "chatbot"])
def test_subjective_templates_ship_llm_judge_example(template: str, tmp_path: Path) -> None:
    """Templates whose output is open-ended natural language ship a
    judge.yaml.example configured for LLM-as-judge — exact-match won't
    score them. Chatbot joined the list with the chatbot template
    (Tier-1 #1 follow-up)."""
    dst = tmp_path / template
    scaffold_agent(dst, name="demo", template=template)
    assert (dst / "evals" / "judge.yaml.example").is_file()
    assert _load_judge_example_method(dst) is JudgeMethod.LLM_JUDGE


@pytest.mark.unit
@pytest.mark.parametrize("template", ["classifier", "extractor"])
def test_deterministic_templates_ship_exact_match_judge_example(
    template: str, tmp_path: Path
) -> None:
    """Templates whose output is a fixed-shape typed value (finite-label
    classifier, structured-field extractor) work fine with exact-match
    scoring. Under the canonical layout (#127) they still ship a
    judge.yaml.example — but one configured for ``method: exact`` rather
    than an LLM judge, so the example matches what the dataset implies."""
    dst = tmp_path / template
    scaffold_agent(dst, name="demo", template=template)
    assert (dst / "evals" / "judge.yaml.example").is_file()
    assert _load_judge_example_method(dst) is JudgeMethod.EXACT


# ---------------------------------------------------------------------------
# hr-policy eval is valid + meaningful (regression for #121)
# ---------------------------------------------------------------------------
#
# The hr-policy template shipped a broken eval that scored 0/N:
#   1. dataset rows carried a stray ``file_format_under_test`` field, and
#   2. agent.yaml declared a dataset but no judge → the harness fell back
#      to exact-match, which can never pass a free-text HR-policy answer.
# These tests pin both fixes: the dataset only uses the recognized eval-row
# schema, and the shipped judge.yaml is an LLM-as-judge (so a correct answer
# scores non-zero) rather than the exact-match default.

# Fields the eval-row loader recognizes (movate.core.eval._parse_dataset_path).
# A row carrying anything outside this set has a stray field.
_ALLOWED_EVAL_ROW_KEYS = {
    "input",
    "expected",
    "tags",
    "objective",
    "grounding",
    "expected_coverage",
    "latency_budget_ms",
    "skill_responses",
    "refusal_expected",
    "required_fields",
    "expected_tool_calls",
    "kb_query",
}


@pytest.mark.unit
def test_hr_policy_dataset_has_no_stray_fields(tmp_path: Path) -> None:
    """Every hr-policy dataset row uses only the recognized eval-row schema.

    Regression for #121 problem 1: rows had a stray
    ``file_format_under_test`` field that isn't part of the eval schema.
    """
    dst = tmp_path / "hr-policy"
    scaffold_agent(dst, name="demo", template="hr-policy")
    raw = (dst / "evals" / "dataset.jsonl").read_bytes().decode().splitlines()
    rows = [json.loads(line) for line in raw if line.strip()]
    assert len(rows) >= 1
    for i, row in enumerate(rows, start=1):
        assert "input" in row and "expected" in row, f"row {i} missing input/expected"
        stray = set(row) - _ALLOWED_EVAL_ROW_KEYS
        assert not stray, f"row {i} has stray top-level field(s): {stray}"
        # Input + expected only carry the agent's schema fields.
        assert set(row["input"]) <= {"question", "context"}, f"row {i} stray input field"
        assert set(row["expected"]) <= {
            "answer",
            "citations",
            "grounded",
            "confidence",
            "needs_escalation",
        }, f"row {i} stray expected field"


@pytest.mark.unit
def test_hr_policy_ships_semantic_judge(tmp_path: Path) -> None:
    """hr-policy resolves to an LLM-as-judge, cross-family from the agent.

    Regression for #121 problem 2: with no judge config the harness fell
    back to exact-match scoring (``JudgeMethod.EXACT``), which can never
    pass free-text prose. A shipped ``judge.yaml`` (auto-discovered by the
    ``evals/judge.yaml`` convention) makes scoring semantic.
    """
    dst = tmp_path / "hr-policy"
    scaffold_agent(dst, name="demo", template="hr-policy")
    bundle = load_agent(dst)

    judge = load_judge_config(bundle)
    assert judge.method is JudgeMethod.LLM_JUDGE, "must not fall back to exact-match"
    assert judge.model is not None and judge.rubric, "llm_judge needs a model + rubric"
    # Judge must be a different family than the agent (engine enforces this).
    assert_cross_family(bundle.spec.model.provider, judge.model.provider)


@pytest.mark.unit
async def test_hr_policy_eval_scores_non_zero_with_correct_answer(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """End-to-end: a correct hr-policy answer passes the shipped judge.

    Uses :class:`JudgeStubProvider` so no live API key is needed — the
    agent provider (openai) returns a schema-valid answer and the judge
    provider (anthropic, from the shipped judge.yaml) returns a passing
    score. Proves the eval is wired to the semantic judge and yields a
    non-zero pass rather than the 0/N exact-match it shipped with.
    """
    dst = tmp_path / "hr-policy"
    scaffold_agent(dst, name="demo", template="hr-policy")
    bundle = load_agent(dst)

    _, mock_response = CANONICAL["hr-policy"]
    provider = JudgeStubProvider(agent_response=mock_response, judge_score=0.95)
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    assert summary.sample_count == 18
    assert summary.judge_provider == "anthropic/claude-sonnet-4-6"
    assert summary.pass_rate == 1.0
    assert summary.mean_score == pytest.approx(0.95)
    # Both the agent (openai) and judge (anthropic) providers were exercised.
    assert any(c.startswith("openai/") for c in provider.calls)
    assert any(c.startswith("anthropic/") for c in provider.calls)


# ---------------------------------------------------------------------------
# Conformance guard — EVERY bundled agent template (not just the registered
# ones) must match the canonical layout (#127). This walks the templates dir
# on disk so a future template that ships inline / JSON schema, or forgets a
# judge.yaml.example, fails CI here rather than drifting silently.
# ---------------------------------------------------------------------------


def _bundled_agent_template_dirs() -> list[Path]:
    """Every directory under ``src/movate/templates`` that ships an
    ``agent.yaml`` — registered shapes, role templates, and any
    reference-only dirs (e.g. ``roles/sql-writer``). Skill (``skill.yaml``)
    and tool (``tool.yaml``) templates are deliberately excluded: the
    canonical AGENT layout doesn't apply to them."""
    return sorted(
        (p.parent for p in TEMPLATES_DIR.rglob("agent.yaml")),
        key=lambda d: d.relative_to(TEMPLATES_DIR).as_posix(),
    )


def test_bundled_agent_template_dirs_are_discovered() -> None:
    """Sanity: the on-disk walk finds the agent templates we expect. Guards
    the guard — if the glob silently matched nothing, the conformance test
    below would vacuously pass."""
    dirs = _bundled_agent_template_dirs()
    names = {d.relative_to(TEMPLATES_DIR).as_posix() for d in dirs}
    # Registered shapes resolve to these dirs; roles/* + the reference-only
    # roles/sql-writer ship agent.yaml too.
    assert "agent_init" in names
    assert "faq_agent" in names
    assert "roles/support-triage" in names
    assert "roles/sql-writer" in names  # reference-only, still must conform
    # ~24 agent templates today; lower bound keeps the assertion stable as
    # new templates land.
    assert len(dirs) >= 20


@pytest.mark.unit
@pytest.mark.parametrize(
    "template_dir",
    _bundled_agent_template_dirs(),
    ids=lambda d: d.relative_to(TEMPLATES_DIR).as_posix(),
)
def test_bundled_agent_template_conforms_to_canonical_layout(template_dir: Path) -> None:
    """Every bundled agent template ships the canonical layout (#127):

        agent.yaml  — schema refs are FILE paths to ./schema/{input,output}.yaml
        prompt.md
        evals/dataset.jsonl
        evals/judge.yaml.example
        schema/input.yaml   (canonical YAML — never inline, never .json)
        schema/output.yaml

    This is the regression guard the unification PR adds so future
    templates can't reintroduce inline / JSON schema or drop the
    judge.yaml.example.
    """
    name = template_dir.relative_to(TEMPLATES_DIR).as_posix()

    assert (template_dir / "agent.yaml").is_file(), f"{name}: missing agent.yaml"
    assert (template_dir / "prompt.md").is_file(), f"{name}: missing prompt.md"
    assert (template_dir / "evals" / "dataset.jsonl").is_file(), f"{name}: missing dataset"
    assert (template_dir / "evals" / "judge.yaml.example").is_file(), (
        f"{name}: missing evals/judge.yaml.example"
    )

    assert (template_dir / "schema" / "input.yaml").is_file(), f"{name}: missing schema/input.yaml"
    assert (template_dir / "schema" / "output.yaml").is_file(), (
        f"{name}: missing schema/output.yaml"
    )
    assert not (template_dir / "schema" / "input.json").exists(), f"{name}: stray schema/input.json"
    assert not (template_dir / "schema" / "output.json").exists(), (
        f"{name}: stray schema/output.json"
    )

    spec = yaml.safe_load((template_dir / "agent.yaml").read_text())
    schema_block = spec.get("schema", {})
    assert schema_block.get("input") == "./schema/input.yaml", (
        f"{name}: schema.input must be the file ref ./schema/input.yaml "
        f"(got {schema_block.get('input')!r} — inline or JSON schema is not canonical)"
    )
    assert schema_block.get("output") == "./schema/output.yaml", (
        f"{name}: schema.output must be the file ref ./schema/output.yaml "
        f"(got {schema_block.get('output')!r} — inline or JSON schema is not canonical)"
    )

    # Both canonical YAML schema files compile to valid JSON Schema. (Full
    # end-to-end `load_agent` — incl. skill/context/kb resolution — is
    # covered by test_scaffold_each_template_loads / the role-template
    # tests, which run the proper project-scaffold flow; here we only
    # assert the schema-location convention + that the files are valid.)
    for label in ("input", "output"):
        compiled = _load_schema_doc(template_dir / "schema" / f"{label}.yaml", label=label)
        assert compiled.get("type") == "object", f"{name}: {label} schema is not an object schema"

    # The judge example parses and is a recognized scoring method.
    judge_doc = yaml.safe_load((template_dir / "evals" / "judge.yaml.example").read_text())
    JudgeConfig(**judge_doc)
