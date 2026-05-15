"""Audit scanners + orchestrator.

Each scanner is a **pure function**: ``(agent_dir: Path) -> list[Finding]``.
Scanners don't know about each other; they're orthogonal. The
orchestrator (:func:`audit_current` / :func:`audit_snapshot`) walks
every agent and runs every registered scanner.

Adding a new scanner = drop a function with the ``@register`` decorator.
The CLI auto-discovers it.

Scanner conventions:

* **Pure** — no I/O outside reading the agent's own files. No
  network. No mutation.
* **One concern per scanner** — "missing evals" and "missing
  description" are separate scanners. Lets `--category` filter
  granularly.
* **Operator-friendly messages** — "agent.yaml missing `description:` field"
  not "metadata insufficient." Always actionable.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import yaml

from movate.audit.report import AuditReport, Finding, Severity
from movate.snapshot.store import resolve_snapshot, snapshot_path

# Scanner type — caller doesn't care about implementation, just shape.
Scanner = Callable[[Path, str], list[Finding]]

# Registry populated by ``@register``. The orchestrator iterates this
# in declaration order so audit output is deterministic per scanner
# category. Operators can filter via ``--category <name>`` in the CLI.
SCANNERS: dict[str, Scanner] = {}


def register(category: str) -> Callable[[Scanner], Scanner]:
    """Decorator that adds a scanner to :data:`SCANNERS`.

    Using a decorator (not a plain dict assign) so each scanner's
    category lives next to its implementation — a future contributor
    can grep for the category name and find the scanner immediately.
    """

    def decorator(fn: Scanner) -> Scanner:
        SCANNERS[category] = fn
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_agent_yaml(agent_dir: Path) -> dict | None:
    """Parse agent.yaml; return None if missing/malformed (scanners
    that need the file return early, no spurious crashes)."""
    yaml_path = agent_dir / "agent.yaml"
    if not yaml_path.is_file():
        return None
    try:
        raw = yaml.safe_load(yaml_path.read_text())
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


# ---------------------------------------------------------------------------
# Scanner: missing evals dataset
# ---------------------------------------------------------------------------


@register("missing-evals")
def scan_missing_evals(agent_dir: Path, agent_name: str) -> list[Finding]:
    """Agent has no eval dataset → can't be scored in CI."""
    dataset = agent_dir / "evals" / "dataset.jsonl"
    if dataset.is_file():
        return []
    return [
        Finding(
            category="missing-evals",
            severity=Severity.ERROR,
            target=agent_name,
            message="no eval dataset (`evals/dataset.jsonl` missing)",
            hint=(
                "add at least 2-3 cases — `mdk eval gen` will land in "
                "Sprint R to bootstrap one from a sample input"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Scanner: missing description
# ---------------------------------------------------------------------------


@register("missing-description")
def scan_missing_description(agent_dir: Path, agent_name: str) -> list[Finding]:
    """Agent without a description is opaque in the marketplace + CLI."""
    raw = _load_agent_yaml(agent_dir)
    if raw is None:
        return []
    description = str(raw.get("description") or "").strip()
    if description:
        return []
    return [
        Finding(
            category="missing-description",
            severity=Severity.WARNING,
            target=agent_name,
            message="agent.yaml missing `description:` field",
            hint=(
                "add a 1-2 sentence summary so `mdk add --describe` + Mova iO "
                "catalog render meaningfully"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Scanner: missing owner
# ---------------------------------------------------------------------------


@register("missing-owner")
def scan_missing_owner(agent_dir: Path, agent_name: str) -> list[Finding]:
    """An agent without an owner has nobody to page when it breaks."""
    raw = _load_agent_yaml(agent_dir)
    if raw is None:
        return []
    owner = str(raw.get("owner") or "").strip()
    if owner:
        return []
    return [
        Finding(
            category="missing-owner",
            severity=Severity.WARNING,
            target=agent_name,
            message="agent.yaml missing `owner:` field",
            hint=(
                "add the team or person responsible (e.g. "
                "'support-platform@company') for on-call routing"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Scanner: exposed secrets in agent.yaml + prompt.md
# ---------------------------------------------------------------------------

# Regex patterns for common leaked-secret shapes. Conservative —
# false-positive on hand-edited prose is worse than a missed secret
# (we have the `mdk guardrails` engine to catch real PII / secrets
# in input/output at runtime; THIS scanner is about
# committed-to-VCS leaks).
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # OpenAI keys: sk-...
    ("openai-api-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    # Anthropic keys: sk-ant-...
    ("anthropic-api-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    # GitHub PATs: ghp_, ghs_, gho_, ghu_, ghr_
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    # AWS access keys: AKIA[0-9A-Z]{16}
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    # MDK bearer tokens: mvt_<env>_<tenant>_<keyid>_<secret>
    ("mdk-bearer-token", re.compile(r"mvt_(live|dev|staging)_[A-Za-z0-9_-]{20,}")),
    # Generic long base64-ish strings — last-resort heuristic. Very
    # opt-in: requires the literal word "secret"/"token"/"key" within
    # 20 chars of the suspicious blob, to limit false positives.
)


@register("exposed-secret")
def scan_exposed_secrets(agent_dir: Path, agent_name: str) -> list[Finding]:
    """Regex scan of agent.yaml + prompt.md for committed-credential shapes.

    Catches the obvious mistake: pasting an API key into a prompt
    template for testing and forgetting to remove it. Doesn't catch
    every leak (we don't try to be a full DLP) — pairs with the J-0
    guardrails for runtime PII / secret redaction.
    """
    findings: list[Finding] = []
    for filename in ("agent.yaml", "prompt.md"):
        path = agent_dir / filename
        if not path.is_file():
            continue
        try:
            content = path.read_text()
        except OSError:
            continue
        for label, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(content):
                # Trim the matched value for the message so we don't
                # echo the full key (defense in depth — even our own
                # error output shouldn't surface the leaked key).
                preview = match.group()[:8] + "..." + match.group()[-4:]
                findings.append(
                    Finding(
                        category="exposed-secret",
                        severity=Severity.ERROR,
                        target=f"{agent_name}/{filename}",
                        message=f"possible {label} found: {preview}",
                        hint=(
                            "rotate the key immediately, then move it to "
                            "an env var or `mdk secrets` (Sprint O)"
                        ),
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Scanner: empty prompt
# ---------------------------------------------------------------------------


@register("empty-prompt")
def scan_empty_prompt(agent_dir: Path, agent_name: str) -> list[Finding]:
    """Prompt that's empty or whitespace-only will produce garbage."""
    prompt_path = agent_dir / "prompt.md"
    if not prompt_path.is_file():
        return []
    body = prompt_path.read_text().strip()
    if body:
        return []
    return [
        Finding(
            category="empty-prompt",
            severity=Severity.ERROR,
            target=agent_name,
            message="prompt.md is empty",
            hint=(
                "agent will produce uninstructed output — add the task "
                "description, schema instructions, and examples"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Scanner: no test signal (no examples AND no dataset)
# ---------------------------------------------------------------------------


@register("no-test-signal")
def scan_no_test_signal(agent_dir: Path, agent_name: str) -> list[Finding]:
    """Agent has neither examples nor an eval dataset. Untestable.

    Distinct from `missing-evals` (which only checks dataset
    existence). This is the stronger claim: there's no test signal
    of any kind. Promoted to error because untestable agents can't
    be safely deployed.
    """
    raw = _load_agent_yaml(agent_dir)
    if raw is None:
        return []
    has_examples = bool(raw.get("examples"))
    dataset = agent_dir / "evals" / "dataset.jsonl"
    has_dataset = dataset.is_file()
    if has_examples or has_dataset:
        return []
    return [
        Finding(
            category="no-test-signal",
            severity=Severity.ERROR,
            target=agent_name,
            message="no examples AND no eval dataset — agent is untestable",
            hint="add `examples:` (at least 1) or an eval dataset; both is recommended",
        )
    ]


# ---------------------------------------------------------------------------
# v2 scanners (Sprint S extensions)
# ---------------------------------------------------------------------------


@register("floating-model-tag")
def scan_floating_model_tag(agent_dir: Path, agent_name: str) -> list[Finding]:
    """Model references a floating tag (``:latest`` / ``:stable``).

    Silent provider rotations on a floating tag can change behavior
    overnight. AgentSpec already rejects this at load time, but the
    audit catches it on shapes we don't validate (e.g. an agent.yaml
    not loaded via the canonical loader). Defense in depth.
    """
    raw = _load_agent_yaml(agent_dir)
    if raw is None:
        return []
    model = raw.get("model") or {}
    provider = str(model.get("provider") or "") if isinstance(model, dict) else ""
    bad_tokens = (":latest", ":stable", ":nightly", ":head", ":main")
    if not any(tok in provider for tok in bad_tokens):
        return []
    return [
        Finding(
            category="floating-model-tag",
            severity=Severity.ERROR,
            target=agent_name,
            message=f"model {provider!r} uses a floating tag — pin a real version",
            hint="floating tags can rotate silently. Pin (e.g. gpt-4o-mini-2024-07-18)",
        )
    ]


@register("missing-version")
def scan_missing_version(agent_dir: Path, agent_name: str) -> list[Finding]:
    """Agent.yaml lacks a ``version`` field.

    Without a version, snapshot diffs / promotion tracking can't tell
    "did the operator actually bump?" from "operator forgot to bump."
    Warning rather than error — many internal agents skip it.
    """
    raw = _load_agent_yaml(agent_dir)
    if raw is None:
        return []
    if raw.get("version"):
        return []
    return [
        Finding(
            category="missing-version",
            severity=Severity.WARNING,
            target=agent_name,
            message="agent.yaml has no `version` field",
            hint="add `version: 0.1.0` and bump on each prompt / model change",
        )
    ]


@register("missing-fallback")
def scan_missing_fallback(agent_dir: Path, agent_name: str) -> list[Finding]:
    """No fallback model declared.

    Agents that go to prod without a fallback fail open when the
    primary provider has an outage. Warning, not error — short-lived
    dev agents legitimately don't need one.
    """
    raw = _load_agent_yaml(agent_dir)
    if raw is None:
        return []
    model = raw.get("model")
    if not isinstance(model, dict):
        return []
    fallback = model.get("fallback")
    if fallback:  # list non-empty
        return []
    return [
        Finding(
            category="missing-fallback",
            severity=Severity.WARNING,
            target=agent_name,
            message="no `model.fallback` declared — primary outage = agent down",
            hint=(
                "add `model.fallback: [{provider: anthropic/claude-haiku-4-5-20251001}]` "
                "(or an equivalent cross-family backup)"
            ),
        )
    ]


@register("prompt-too-long")
def scan_prompt_too_long(agent_dir: Path, agent_name: str) -> list[Finding]:
    """Prompt template is unusually long (> 8000 chars).

    Long prompts inflate cost on every call. Pre-prompt-engineering
    rule of thumb: anything over ~8k chars is a candidate for
    refactoring into per-call context retrieval. Warning so operators
    can decide.
    """
    prompt_path = agent_dir / "prompt.md"
    if not prompt_path.is_file():
        return []
    chars = len(prompt_path.read_text())
    threshold = 8000
    if chars <= threshold:
        return []
    return [
        Finding(
            category="prompt-too-long",
            severity=Severity.WARNING,
            target=agent_name,
            message=f"prompt.md is {chars:,} chars (> {threshold:,})",
            hint=(
                "consider splitting into shared contexts (./contexts/*.md) "
                "or trimming examples — long prompts pay per-call"
            ),
        )
    ]


@register("schema-no-required")
def scan_schema_no_required(agent_dir: Path, agent_name: str) -> list[Finding]:
    """Input schema declares no ``required`` fields.

    Optional-only schemas accept ``{}`` as input, which usually means
    the agent has no constraints — operators wrap whatever and hope.
    Warning so operators tighten the schema or explicitly accept the
    loose contract.
    """
    raw = _load_agent_yaml(agent_dir)
    if raw is None:
        return []
    schema = raw.get("schema") or {}
    input_block = schema.get("input") if isinstance(schema, dict) else None
    # Inline shorthand uses key-form with `?` for optional; the absence
    # of any non-`?` key implies no required fields.
    if isinstance(input_block, dict):
        has_required = any(not str(k).endswith("?") for k in input_block)
        if has_required:
            return []
    elif isinstance(input_block, str):
        # Path-form — operators using a full JSON Schema can audit the
        # file separately. Skip rather than guess.
        return []
    else:
        return []
    return [
        Finding(
            category="schema-no-required",
            severity=Severity.WARNING,
            target=agent_name,
            message="input schema has no required fields — accepts {}",
            hint="mark at least one field non-optional (drop the trailing `?`)",
        )
    ]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _run_scanners(
    *,
    agents_root: Path,
    categories: list[str] | None = None,
) -> AuditReport:
    """Walk every agent in ``agents_root`` and run every (filtered) scanner.

    ``categories`` filter is intersection: if set, only scanners with
    a category in the list run. None / empty = run all scanners.
    """
    if not agents_root.is_dir():
        return AuditReport(findings=(), scanned_agents=0)

    active = (
        SCANNERS if not categories else {k: v for k, v in SCANNERS.items() if k in set(categories)}
    )

    findings: list[Finding] = []
    agent_count = 0
    for agent_dir in sorted(agents_root.iterdir()):
        if not agent_dir.is_dir() or not (agent_dir / "agent.yaml").is_file():
            continue
        agent_count += 1
        agent_name = agent_dir.name
        for scanner in active.values():
            findings.extend(scanner(agent_dir, agent_name))

    return AuditReport(findings=tuple(findings), scanned_agents=agent_count)


def audit_current(
    project_root: Path,
    *,
    categories: list[str] | None = None,
) -> AuditReport:
    """Scan the live project state at ``project_root``."""
    return _run_scanners(
        agents_root=project_root / "agents",
        categories=categories,
    )


def audit_snapshot(
    project_root: Path,
    target_hash: str,
    *,
    categories: list[str] | None = None,
) -> AuditReport:
    """Scan a captured snapshot's agents/ directory.

    Resolves the snapshot via the snapshot store, then runs the
    same scanners against its ``files/agents/`` mirror. Lets
    operators audit a snapshot BEFORE running ``mdk promote``
    (Sprint O) so failed-audit snapshots never make it to staging.
    """
    manifest = resolve_snapshot(project_root, target_hash)
    short = manifest.hash.removeprefix("sha256:")[:8]
    agents_root = snapshot_path(project_root, short) / "files" / "agents"
    report = _run_scanners(agents_root=agents_root, categories=categories)
    return report
