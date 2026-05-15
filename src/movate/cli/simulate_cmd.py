"""``mdk simulate <chatbot>`` — multi-turn conversation simulator (Sprint R).

Stress-tests a chatbot before production by running it through a list
of scenarios — each a persona + goal + initial message. The "user"
side of the conversation is simulated by an LLM that knows the
persona + goal and tries to drive the conversation toward it.
Operators read the resulting transcripts to spot:

* Chatbots that lose the thread after N turns
* Refusals where the bot should help
* Help where the bot should refuse
* Loops, repetitions, hallucinations

Where ``mdk eval`` checks individual input→output pairs, simulate
checks the **conversational arc** — the surface area that single-turn
evals miss.

Usage::

  mdk simulate triage --num 3                    # generate 3 scenarios + run
  mdk simulate triage --scenarios scenarios.jsonl  # use a curated set
  mdk simulate triage --num 3 --mock              # offline / hermetic
  mdk simulate triage --num 5 --max-turns 8       # tight turn cap
  mdk simulate triage --num 3 --output results.json  # save transcripts

Scenario shape (when loaded from JSONL):

    {
      "persona": "an annoyed customer whose order is 3 weeks late",
      "goal": "get a refund or a clear date when the order will ship",
      "initial_message": "where is my order??",
      "max_turns": 6
    }

[bold]Design call — what counts as "pass":[/bold] MVP uses a
heuristic: a scenario "passes" if the chatbot stayed on-topic +
the simulated user (or a judge LLM at the end) signals goal
achieved. Real pass/fail definitions are noisy in chatbot evals;
operators read the transcripts. The pass column is a hint, not a
verdict.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.loader import AgentLoadError, load_agent
from movate.core.models import RunRequest
from movate.providers.base import CompletionRequest, Message

console = Console()
err_console = Console(stderr=True)


# Defaults + caps. Caps keep `mdk simulate` from accidentally launching
# a 100-scenario, 20-turn session that costs serious money.
_DEFAULT_NUM_SCENARIOS = 3
_DEFAULT_MAX_TURNS = 6
_MAX_SCENARIOS = 50
_MAX_TURNS_CAP = 20

# Pass-rate threshold for the summary color flip (green vs yellow).
# Operator-facing — surfaces "more than half passed" at a glance.
_PASS_RATE_GREEN_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One conversation scenario — persona + goal + opening message.

    The persona is what the simulated user "is" (background, mood);
    the goal is what they're trying to achieve. The initial message
    kicks off the conversation. ``max_turns`` caps how long we let
    each scenario run before declaring inconclusive.
    """

    persona: str
    goal: str
    initial_message: str
    max_turns: int = _DEFAULT_MAX_TURNS


@dataclass
class Turn:
    """One turn in a transcribed conversation."""

    role: str  # "user" | "assistant"
    content: str


@dataclass
class SimulationResult:
    """Outcome of running one scenario against the chatbot."""

    scenario: Scenario
    transcript: list[Turn] = field(default_factory=list)
    turns_used: int = 0
    goal_achieved: bool = False
    notes: str = ""


# ---------------------------------------------------------------------------
# Built-in scenario generator (mock + LLM modes)
# ---------------------------------------------------------------------------


# Stock scenarios used by --mock. Cover common chatbot stress patterns
# (clarification, refusal, on-topic) without LLM call dependencies.
_MOCK_SCENARIOS = (
    Scenario(
        persona="a confused user who isn't sure what they need",
        goal="figure out which product fits their use case",
        initial_message="i need help picking something",
    ),
    Scenario(
        persona="an impatient user who wants a fast answer",
        goal="get a one-sentence response",
        initial_message="just tell me the answer",
    ),
    Scenario(
        persona="a curious user testing edge cases",
        goal="see how the bot handles ambiguous questions",
        initial_message="what about that thing?",
    ),
    Scenario(
        persona="an angry user with a complaint",
        goal="get acknowledgment + a path to resolution",
        initial_message="this is ridiculous and i want to talk to a person",
    ),
    Scenario(
        persona="a polite first-time user",
        goal="learn what the bot is good for",
        initial_message="hi, what can you help me with?",
    ),
)


_SCENARIO_GEN_PROMPT = """\
You generate VARIED test scenarios for a chatbot. Each scenario has:
- persona: a one-sentence character description
- goal: what the persona is trying to achieve
- initial_message: what they'd say first

Produce exactly ONE scenario as a JSON object with those three keys.
Vary across calls: try different moods, topics, and goal types
(information seeking, refunds, complaints, complex multi-step asks).

Respond with a single JSON object — no markdown, no prose, no code
fences. Just the bare JSON object.
"""


def _mock_scenarios(num: int) -> list[Scenario]:
    """Pick N scenarios from the stock list, cycling if N > stock size."""
    stock = list(_MOCK_SCENARIOS)
    return [stock[i % len(stock)] for i in range(num)]


async def _generate_scenarios(rt: Any, num: int) -> list[Scenario]:
    """Ask the LLM to invent N scenarios. Falls back to mock on errors
    so one network blip doesn't kill the whole simulation."""
    provider = rt.provider
    scenarios: list[Scenario] = []
    for i in range(num):
        request = CompletionRequest(
            provider="openai/gpt-4o-mini-2024-07-18",
            messages=[
                Message(role="system", content=_SCENARIO_GEN_PROMPT),
                Message(role="user", content=f"Generate scenario #{i + 1}."),
            ],
            params={"temperature": 0.9, "max_tokens": 300},
        )
        try:
            response = await provider.complete(request)
            scenario = _parse_scenario_json(response.text)
        except Exception:
            scenario = _MOCK_SCENARIOS[i % len(_MOCK_SCENARIOS)]
        scenarios.append(scenario)
    return scenarios


_FENCE_RE = re.compile(r"^```(?:json)?\s*|```\s*$", re.MULTILINE)


def _parse_scenario_json(raw: str) -> Scenario:
    """Lift LLM-generated JSON into a Scenario, fallback-safe."""
    text = _FENCE_RE.sub("", raw).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return Scenario(
            persona="generic test user",
            goal="see what the bot can do",
            initial_message="hello",
        )
    if not isinstance(data, dict):
        return Scenario(
            persona="generic test user",
            goal="see what the bot can do",
            initial_message="hello",
        )
    return Scenario(
        persona=str(data.get("persona") or "test user"),
        goal=str(data.get("goal") or "interact with the bot"),
        initial_message=str(data.get("initial_message") or "hello"),
    )


# ---------------------------------------------------------------------------
# Conversation engine
# ---------------------------------------------------------------------------


_USER_SIDE_PROMPT = """\
You are role-playing a user talking to a customer-service chatbot.

Persona: {persona}
Your goal: {goal}

Reply in the FIRST PERSON as that user. Keep messages short and
realistic — 1-2 sentences. If the chatbot meets your goal, end your
reply with the literal marker [GOAL_ACHIEVED]. If you've decided the
conversation is going nowhere, end with [GIVE_UP].

Do NOT add commentary, narration, or stage directions — just your
next message as the user.
"""


async def _simulated_user_turn(rt: Any, scenario: Scenario, transcript: list[Turn]) -> str:
    """Ask the user-LLM for the next message given the conversation so far."""
    provider = rt.provider
    history = [
        Message(
            role="system",
            content=_USER_SIDE_PROMPT.format(persona=scenario.persona, goal=scenario.goal),
        ),
    ]
    # The simulated user sees the conversation from the OPPOSITE side:
    # the chatbot's outputs are "assistant" from the user's POV become
    # "user" messages they're responding to. We invert roles for the
    # history feed to the user-LLM.
    for turn in transcript:
        role = "assistant" if turn.role == "user" else "user"
        history.append(Message(role=role, content=turn.content))

    request = CompletionRequest(
        provider="openai/gpt-4o-mini-2024-07-18",
        messages=history,
        params={"temperature": 0.7, "max_tokens": 200},
    )
    try:
        response = await provider.complete(request)
    except Exception as exc:
        err_console.print(f"[yellow]⚠[/yellow] simulated-user LLM call failed: {exc}")
        return "[GIVE_UP]"
    return (response.text or "[GIVE_UP]").strip()


_GOAL_MARKER = "[GOAL_ACHIEVED]"
_GIVE_UP_MARKER = "[GIVE_UP]"


def _is_goal_achieved(user_message: str) -> bool:
    return _GOAL_MARKER in user_message


def _is_give_up(user_message: str) -> bool:
    return _GIVE_UP_MARKER in user_message


def _strip_markers(text: str) -> str:
    """Remove the meta markers before showing the transcript to humans."""
    return text.replace(_GOAL_MARKER, "").replace(_GIVE_UP_MARKER, "").strip()


async def _run_one_scenario(
    rt: Any,
    bundle: Any,
    scenario: Scenario,
    *,
    mock: bool,
) -> SimulationResult:
    """Drive one scenario through the chatbot until goal / give-up / max-turns."""
    transcript: list[Turn] = []
    notes = ""

    # Seed turn 1: the persona's initial message.
    transcript.append(Turn(role="user", content=scenario.initial_message))

    achieved = False
    turns_used = 0
    for _ in range(scenario.max_turns):
        # ---- Chatbot turn ----
        # Pass the latest user message as the agent's input. Most
        # chatbot templates take a `message` or `text` field; we try
        # the schema's first required field as the input key.
        input_field = _pick_chat_input_key(bundle.input_schema)
        last_user = transcript[-1].content
        request = RunRequest(agent=bundle.spec.name, input={input_field: last_user})
        try:
            response = await rt.executor.execute(bundle, request)
        except Exception as exc:
            notes = f"chatbot crashed: {exc}"
            break

        bot_reply = _flatten_response(response.data)
        transcript.append(Turn(role="assistant", content=bot_reply))
        turns_used += 1

        # ---- Simulated user turn ----
        if mock:
            # In mock mode, after one round-trip, declare goal achieved
            # so the transcript loop exits deterministically.
            achieved = True
            break

        user_reply = await _simulated_user_turn(rt, scenario, transcript)
        if _is_goal_achieved(user_reply):
            achieved = True
            transcript.append(Turn(role="user", content=_strip_markers(user_reply)))
            break
        if _is_give_up(user_reply):
            transcript.append(Turn(role="user", content=_strip_markers(user_reply)))
            notes = "simulated user gave up"
            break
        transcript.append(Turn(role="user", content=user_reply))

    if turns_used == scenario.max_turns and not achieved and not notes:
        notes = f"hit max_turns ({scenario.max_turns})"

    return SimulationResult(
        scenario=scenario,
        transcript=transcript,
        turns_used=turns_used,
        goal_achieved=achieved,
        notes=notes,
    )


def _pick_chat_input_key(input_schema: dict[str, Any]) -> str:
    """Choose which field to put the user message in.

    Most chatbot templates use ``message``, ``text``, or ``question``.
    We prefer those by name; otherwise fall back to the first required
    string field. Last resort: ``message`` (so the dispatch always has
    an input shape, even if the agent's executor will then validate-fail).
    """
    preferred = ("message", "text", "question", "input", "query", "prompt")
    props = input_schema.get("properties", {}) or {}
    for name in preferred:
        if name in props:
            return name
    required = input_schema.get("required") or []
    for name in required:
        prop = props.get(name, {})
        if prop.get("type") == "string":
            return str(name)
    return "message"


def _flatten_response(data: Any) -> str:
    """Best-effort: pluck a text reply out of the chatbot's structured output."""
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return str(data)
    for key in ("reply", "response", "answer", "message", "text"):
        if key in data:
            v = data[key]
            return v if isinstance(v, str) else json.dumps(v)
    # Fall back to the whole dict serialized — keeps the transcript
    # readable even for unusual output shapes.
    return json.dumps(data)


# ---------------------------------------------------------------------------
# Scenario file loading
# ---------------------------------------------------------------------------


def _load_scenarios(path: Path) -> list[Scenario]:
    """Parse a JSONL scenarios file. One scenario per line."""
    scenarios: list[Scenario] = []
    try:
        for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise typer.BadParameter(f"line {lineno} is not valid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise typer.BadParameter(f"line {lineno} must be a JSON object")
            scenarios.append(
                Scenario(
                    persona=str(obj.get("persona") or "test user"),
                    goal=str(obj.get("goal") or "interact with the bot"),
                    initial_message=str(obj.get("initial_message") or "hello"),
                    max_turns=int(obj.get("max_turns") or _DEFAULT_MAX_TURNS),
                )
            )
    except OSError as exc:
        raise typer.BadParameter(f"could not read {path}: {exc}") from exc
    if not scenarios:
        raise typer.BadParameter(f"{path} has no scenarios")
    return scenarios


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_summary(results: list[SimulationResult]) -> None:
    table = Table(title="Simulation results", title_style="bold", show_lines=True)
    table.add_column("#", no_wrap=True, style="dim")
    table.add_column("Persona", style="cyan")
    table.add_column("Goal", style="dim")
    table.add_column("Turns", justify="right", no_wrap=True)
    table.add_column("Outcome", no_wrap=True)
    table.add_column("Notes", style="dim")

    passed = 0
    for i, result in enumerate(results, start=1):
        if result.goal_achieved:
            outcome = "[green]✓ achieved[/green]"
            passed += 1
        else:
            outcome = "[yellow]⚠ inconclusive[/yellow]"
        persona = _truncate(result.scenario.persona, 40)
        goal = _truncate(result.scenario.goal, 50)
        table.add_row(
            str(i),
            persona,
            goal,
            str(result.turns_used),
            outcome,
            result.notes or "—",
        )
    console.print(table)
    rate = passed / len(results) if results else 0.0
    color = "green" if rate >= _PASS_RATE_GREEN_THRESHOLD else "yellow"
    console.print(
        f"\n[bold]Goal-achievement rate:[/bold] "
        f"[{color}]{passed}/{len(results)} ({rate:.0%})[/{color}]"
    )


def _truncate(s: str, max_chars: int) -> str:
    return s if len(s) <= max_chars else s[: max_chars - 1] + "…"


def _serialize_results(results: list[SimulationResult]) -> list[dict[str, Any]]:
    """Convert results to plain dicts for --output JSON / JSONL."""
    out = []
    for r in results:
        out.append(
            {
                "persona": r.scenario.persona,
                "goal": r.scenario.goal,
                "initial_message": r.scenario.initial_message,
                "max_turns": r.scenario.max_turns,
                "turns_used": r.turns_used,
                "goal_achieved": r.goal_achieved,
                "notes": r.notes,
                "transcript": [{"role": t.role, "content": t.content} for t in r.transcript],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _resolve_agent_path(name_or_path: str, project_root: Path) -> Path:
    candidate = Path(name_or_path)
    if candidate.is_dir() and (candidate / "agent.yaml").is_file():
        return candidate.resolve()
    by_name = project_root / "agents" / name_or_path
    if by_name.is_dir() and (by_name / "agent.yaml").is_file():
        return by_name.resolve()
    err_console.print(
        f"[red]✗[/red] agent not found: [bold]{name_or_path}[/bold]. "
        "[dim]Looked under [bold]agents/[/bold] and as a literal path.[/dim]"
    )
    raise typer.Exit(code=2)


def simulate(
    name: str = typer.Argument(
        ...,
        help=(
            "Chatbot agent name (resolved under [bold]agents/<name>[/bold]) "
            "or a literal path to the agent directory."
        ),
        metavar="CHATBOT",
    ),
    num: int = typer.Option(
        _DEFAULT_NUM_SCENARIOS,
        "--num",
        "-n",
        help=(
            f"How many scenarios to run when [bold]--scenarios[/bold] is not given. "
            f"Default {_DEFAULT_NUM_SCENARIOS}; max {_MAX_SCENARIOS}."
        ),
    ),
    scenarios_file: str = typer.Option(
        "",
        "--scenarios",
        help=(
            "Path to a JSONL of scenarios. Each line: "
            "[dim]{persona, goal, initial_message, max_turns}[/dim]. "
            "Overrides [bold]--num[/bold] when provided."
        ),
    ),
    max_turns: int = typer.Option(
        _DEFAULT_MAX_TURNS,
        "--max-turns",
        help=(
            f"Per-scenario turn cap (default {_DEFAULT_MAX_TURNS}; max {_MAX_TURNS_CAP}). "
            "Each scenario from a file may override this via its own [dim]max_turns[/dim]."
        ),
    ),
    output: str = typer.Option(
        "",
        "--output",
        "-o",
        help="Save full transcripts to PATH (JSON). Skipped if empty.",
    ),
    pass_rate_gate: float = typer.Option(
        -1.0,
        "--pass-rate-gate",
        help=(
            "CI gate: exit non-zero if goal-achievement rate < threshold "
            "(0.0 to 1.0). Default -1 = no gate. Example: "
            "[dim]--pass-rate-gate 0.7[/dim] fails if fewer than 70% of "
            "scenarios reached their goal."
        ),
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help=(
            "Run scenarios deterministically with MockProvider — no LLM call. "
            "Each scenario runs one chatbot turn + auto-marks achieved. "
            "Useful for tests + sanity-checking the wiring."
        ),
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        help="Project root (default: cwd). Used to resolve bare agent names.",
        hidden=True,
    ),
) -> None:
    """Stress-test a chatbot by running it through multi-turn scenarios.

    Each scenario is a (persona, goal, initial_message) tuple. A
    simulated "user" LLM drives the conversation toward the goal;
    the chatbot replies; we capture every turn. Operators read
    transcripts to spot chatbots that lose the thread, refuse when
    they shouldn't, or hallucinate.

    Where [bold]mdk eval[/bold] checks single input→output pairs,
    [bold]simulate[/bold] checks the conversational ARC — the surface
    area single-turn evals miss.

    [bold]Examples:[/bold]

      [dim]$ mdk simulate triage --num 3                # 3 generated scenarios[/dim]
      [dim]$ mdk simulate triage --scenarios cases.jsonl[/dim]
      [dim]$ mdk simulate triage --num 5 --max-turns 8 --output sim.json[/dim]
      [dim]$ mdk simulate triage --num 3 --mock         # offline / hermetic[/dim]
    """
    if num < 1:
        err_console.print(f"[red]✗[/red] --num must be ≥ 1; got {num}")
        raise typer.Exit(code=2)
    if num > _MAX_SCENARIOS:
        err_console.print(f"[red]✗[/red] --num {num} exceeds safety cap {_MAX_SCENARIOS}")
        raise typer.Exit(code=2)
    if max_turns < 1 or max_turns > _MAX_TURNS_CAP:
        err_console.print(
            f"[red]✗[/red] --max-turns must be in [1, {_MAX_TURNS_CAP}]; got {max_turns}"
        )
        raise typer.Exit(code=2)

    root = Path(project_root).resolve()
    agent_path = _resolve_agent_path(name, root)
    try:
        bundle = load_agent(agent_path)
    except AgentLoadError as exc:
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    results = asyncio.run(
        _simulate_all(
            bundle=bundle,
            num=num,
            scenarios_file=scenarios_file,
            max_turns=max_turns,
            mock=mock,
        )
    )

    _render_summary(results)

    if output:
        out_path = Path(output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(_serialize_results(results), indent=2))
        console.print(
            Panel(
                f"Transcripts: [cyan]{out_path}[/cyan]",
                title="[green]✓[/green] saved",
                title_align="left",
                border_style="green",
            )
        )

    # CI gate: compare goal-achievement rate against the threshold.
    # -1 (default) disables the gate; 0.0..1.0 are operator-supplied.
    # The threshold is INCLUSIVE so 1.0 = "all scenarios must achieve".
    if pass_rate_gate >= 0.0:
        if not 0.0 <= pass_rate_gate <= 1.0:
            err_console.print(
                f"[red]✗[/red] --pass-rate-gate must be in [0.0, 1.0]; got {pass_rate_gate}"
            )
            raise typer.Exit(code=2)
        passed = sum(1 for r in results if r.goal_achieved)
        rate = passed / len(results) if results else 0.0
        if rate < pass_rate_gate:
            err_console.print(
                f"\n[red]✗ pass-rate gate FAILED[/red] ({rate:.0%} < {pass_rate_gate:.0%})"
            )
            raise typer.Exit(code=1)
        console.print(f"\n[green]✓ pass-rate gate met[/green] ({rate:.0%} ≥ {pass_rate_gate:.0%})")


async def _simulate_all(
    *,
    bundle: Any,
    num: int,
    scenarios_file: str,
    max_turns: int,
    mock: bool,
) -> list[SimulationResult]:
    """Build the scenario list + run them all under one runtime."""
    rt = await build_local_runtime(mock=mock)
    try:
        scenarios: list[Scenario]
        if scenarios_file:
            scenarios = _load_scenarios(Path(scenarios_file).resolve())
        elif mock:
            scenarios = _mock_scenarios(num)
        else:
            scenarios = await _generate_scenarios(rt, num)

        # Apply --max-turns as a per-scenario default unless the
        # scenario file overrode it (we already loaded their value).
        scenarios = [
            Scenario(
                persona=s.persona,
                goal=s.goal,
                initial_message=s.initial_message,
                max_turns=s.max_turns if scenarios_file else max_turns,
            )
            for s in scenarios
        ]

        results: list[SimulationResult] = []
        for s in scenarios:
            result = await _run_one_scenario(rt, bundle, s, mock=mock)
            results.append(result)
        return results
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
