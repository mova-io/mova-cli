"""Backend-agnostic LLM-scaffold preview pipeline (ADR 032 D1).

The **pure preview** behind two surfaces:

* ``mdk init --llm`` (``cli/init.py``) — the operator-facing CLI scaffold
  that writes the generated agent to disk (or renders a Rich ``--dry-run``
  panel).
* ``POST /api/v1/agents/preview`` (``runtime/app.py``) — the Mova iO front-end
  preview endpoint (ADR 032 D1): describe an agent in natural language, get
  back a candidate (``agent.yaml`` + ``prompt.md`` + schemas + sample evals)
  WITHOUT committing the scaffold to disk or to the runtime's storage.

Both call-sites share the same generation-+-validation pipeline so a candidate
the front end previews is byte-identical to what the CLI would have scaffolded
for the same description. This module is the single source of truth for the
preview path so CLI and API can never drift (CLAUDE.md rule 4: factor, don't
duplicate).

Design rules:

* **Pure / backend-agnostic.** No I/O, no ``cli`` import, no concrete provider
  — the LLM call goes through the shipped
  :class:`~movate.providers.base.BaseLLMProvider` seam, and validation writes
  to a temp dir then loads through the existing
  :func:`~movate.core.loader.load_agent`. This is what lets the runtime (which
  must not import ``cli`` — ``cli ⊥ runtime``) reuse it.
* **Read-only.** ``preview_agent_from_description`` NEVER writes the scaffold
  to a persistent destination — it only round-trips through an ephemeral
  ``TemporaryDirectory`` to prove the candidate loads. The caller is
  responsible for committing the result if it chooses.
* **Bounded retry.** One generation retry on a load-validation failure (the
  policy the CLI already runs). Failure modes are surfaced typed
  (:class:`ScaffoldPreviewError` + :class:`PreviewFailureMode`) so the HTTP
  layer can map to the right status without re-parsing free-text errors.
* **Cost forecast.** Every successful preview carries a token total + USD
  cost estimate (``None`` if the model isn't in the pricing table) so the
  front end can show LLM spend at preview time.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from movate.core.agent_schema_utils import check_adr023_retrieval
from movate.core.loader import AgentLoadError, load_agent
from movate.core.models import TokenUsage
from movate.providers.base import BaseLLMProvider
from movate.scaffold import (
    GeneratedAgent,
    LLMScaffoldError,
    write_agent_files,
)

# NOTE: ``generate_agent_from_description`` is imported LAZILY inside
# :func:`_run_pipeline` (not at module load) so test fixtures that monkeypatch
# ``movate.scaffold.generate_agent_from_description`` see their patched
# implementation. A module-level ``from movate.scaffold import …`` binds the
# pre-patch function into this module's namespace and the patch becomes a no-op
# at this call site — which was the regression caught by the
# ``test_init_llm_auto_ingest.py`` suite during the ADR 032 D1 factor-out.


class PreviewProgressEvent(enum.StrEnum):
    """Lifecycle events emitted by the preview pipeline.

    The CLI's ``mdk init --llm`` flow consumes these to render its
    per-attempt spinner / between-attempt warning messages without
    duplicating the retry policy itself. The runtime endpoint passes no
    callback, so these are silently discarded there.

    Events fire in this order on a 2-attempt path that retries:

    1. ``ATTEMPT_STARTED`` — before the LLM call for attempt 1.
    2. ``GENERATION_FAILED`` or ``VALIDATION_FAILED`` — attempt 1 lost; the
       ``message`` argument carries the error string the retry uses for
       feedback (and the operator sees as a yellow warning).
    3. ``ATTEMPT_RETRY_STARTED`` — before the LLM call for attempt 2.
    """

    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_RETRY_STARTED = "attempt_retry_started"
    GENERATION_FAILED = "generation_failed"
    VALIDATION_FAILED = "validation_failed"


# Synchronous observer the CLI uses to drive its Rich console output. Kept
# sync because the CLI's ``spinner()`` context manager is sync — making this
# async would force every CLI emit to ``await``. The signature is
# ``(event, message_or_none)`` where ``message`` is the error string for the
# failure events.
ProgressCallback = Callable[[PreviewProgressEvent, str | None], None]


# Optional CLI-only injection: provision the candidate's declared skills into
# the validation tempdir's project root BEFORE ``load_agent`` runs. Lets the
# CLI's ``--llm`` flow keep the existing F3 (#112) behavior — a RAG-shape
# scaffold declaring ``kb-vector-lookup`` resolves at validate-time because the
# built-in skill is copied alongside. The runtime preview endpoint passes
# ``None`` (it has no project to provision into) and a RAG-shape candidate
# that declares an unresolvable skill will fail validation cleanly — which is
# the right answer for a read-only describe endpoint.
SkillProvisioner = Callable[[GeneratedAgent, Path], None]


class PreviewFailureMode(enum.StrEnum):
    """Typed failure modes for the preview pipeline.

    Used by the runtime endpoint to pick the right HTTP status without
    re-parsing error strings, and by the CLI to choose between the two
    debug-artifact exit codes.

    * ``GENERATION`` — the LLM provider call failed, returned non-JSON, or
      returned JSON that didn't match :class:`GeneratedAgent`'s schema. Both
      attempts (attempt + retry) exhausted in this mode.
    * ``VALIDATION`` — generation produced a parseable :class:`GeneratedAgent`,
      but :func:`load_agent` (or the ADR 023 retrieval cross-check, or the
      sample-evals JSON-Schema check) rejected it on the final attempt.
    * ``EMPTY_DESCRIPTION`` — guard for an empty/whitespace-only description;
      raised before any provider call (operator/UI error, not an LLM error).
    """

    GENERATION = "generation"
    VALIDATION = "validation"
    EMPTY_DESCRIPTION = "empty_description"


@dataclass(frozen=True)
class ScaffoldPreview:
    """A successful preview: the validated candidate + cost forecast.

    Returned by :func:`preview_agent_from_description`. The fields mirror the
    shape both the CLI (Rich preview, success panel) and the HTTP endpoint
    (response body) need to render:

    * ``agent`` — the validated :class:`GeneratedAgent` (``agent_yaml`` +
      ``prompt_md`` + schemas + ``sample_evals``).
    * ``tokens`` — rolled across attempt + retry, so cost reflects total spend.
    * ``cost_usd`` — looked up via the shipped pricing table. ``None`` when the
      model isn't listed; the caller renders that as "N/A".
    * ``retried`` — true if attempt 2 actually fired (a retry happened).
      The CLI surfaces this in its mdk_init_summary line; the front end may
      use it to flag a slightly-flakier-than-usual scaffold.
    * ``target_model`` — the model string the GENERATED agent declares in
      ``agent_yaml.model.provider`` (which can differ from the scaffold-driver
      model passed in ``model=``; see :func:`generate_agent_from_description`).
    """

    agent: GeneratedAgent
    tokens: TokenUsage
    cost_usd: float | None
    retried: bool
    target_model: str


@dataclass
class ScaffoldPreviewError(Exception):
    """Raised by :func:`preview_agent_from_description` on a failed preview.

    Carries the failure mode + the operator-facing error message (the same
    string the CLI's retry feedback uses), plus the partial state at the
    point of failure so a caller can surface a useful debug artifact:

    * ``tokens`` — tokens spent across attempts that ran (always populated;
      zero if the failure happened before any provider call).
    * ``partial_agent`` — the parsed-but-invalid :class:`GeneratedAgent` from
      the final attempt, when ``mode == VALIDATION`` (else ``None``).
    * ``retried`` — whether the second attempt fired.
    """

    mode: PreviewFailureMode
    message: str
    tokens: TokenUsage = field(default_factory=TokenUsage)
    partial_agent: GeneratedAgent | None = None
    retried: bool = False

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.mode.value}] {self.message}"


# ---------------------------------------------------------------------------
# Canonical-defaults gap-fill (shared with CLI)
# ---------------------------------------------------------------------------

# Sensible per-provider fallback target for the scaffolded agent.yaml's
# ``model.fallback``. Keyed by the PRIMARY model's provider prefix (before
# the ``/``); value is a DIFFERENT-family model so a primary outage has
# somewhere to go. Mirrors the CLI's table (``cli/init.py``) — kept in lock-step
# so the CLI scaffold and the HTTP preview emit byte-identical defaults.
_FALLBACK_BY_PROVIDER: dict[str, str] = {
    "openai": "anthropic/claude-haiku-4-5-20251001",
    "azure": "anthropic/claude-haiku-4-5-20251001",
    "anthropic": "openai/gpt-4o-mini-2024-07-18",
    "gemini": "anthropic/claude-haiku-4-5-20251001",
}
_DEFAULT_FALLBACK_MODEL = "anthropic/claude-haiku-4-5-20251001"

# Operational defaults written into every scaffolded agent.yaml so a ``--llm``
# agent matches the hand-init'd field set (``templates/agent_init/agent.yaml``).
_SCAFFOLD_DEFAULT_TIMEOUTS: dict[str, int] = {"call_ms": 30000, "total_ms": 60000}
_SCAFFOLD_DEFAULT_BUDGET: dict[str, float] = {"max_cost_usd_per_run": 0.50}


def apply_canonical_agent_defaults(agent_yaml: dict[str, Any], *, target_model: str) -> None:
    """Fill the canonical operational fields into a generated ``agent_yaml``.

    Mutates ``agent_yaml`` in place. GAP-FILL only — never clobbers a field the
    model already emitted (the RAG shape's ``tags``, an exemplar's ``budget``),
    so shape-specific content is preserved.

    Aligns a ``--llm`` scaffold with a hand-init'd agent
    (``templates/agent_init/agent.yaml``) by ensuring ``model.fallback``,
    ``timeouts``, ``budget``, and ``tags`` are present. ``model.fallback`` is
    derived from the PRIMARY ``target_model``'s provider family so the fallback
    is a different family.
    """
    # model.fallback — only when absent. Pick a different-family target.
    model_block = agent_yaml.get("model")
    if isinstance(model_block, dict) and not model_block.get("fallback"):
        provider_prefix = target_model.split("/", 1)[0] if "/" in target_model else target_model
        fallback_model = _FALLBACK_BY_PROVIDER.get(provider_prefix, _DEFAULT_FALLBACK_MODEL)
        # Drop the degenerate same-model fallback rather than emit a pointless entry.
        if fallback_model != target_model:
            model_block["fallback"] = [{"provider": fallback_model}]

    # timeouts / budget — gap-fill the standard caps.
    if "timeouts" not in agent_yaml:
        agent_yaml["timeouts"] = dict(_SCAFFOLD_DEFAULT_TIMEOUTS)
    if "budget" not in agent_yaml:
        agent_yaml["budget"] = dict(_SCAFFOLD_DEFAULT_BUDGET)

    # tags — ensure the key exists (empty list is the agent_init default).
    if "tags" not in agent_yaml:
        agent_yaml["tags"] = []


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _accumulate_tokens(running: TokenUsage, new: TokenUsage) -> TokenUsage:
    """Sum two :class:`TokenUsage` values into a fresh instance (Pydantic
    models don't support ``__add__``)."""
    return TokenUsage(
        input=running.input + new.input,
        output=running.output + new.output,
        cached_input=running.cached_input + new.cached_input,
    )


def _validate_sample_evals(generated: GeneratedAgent) -> str | None:
    """Validate each present ``sample_evals`` row against the generated I/O
    schemas, using the same JSON-Schema 2020-12 validator the runtime uses.

    Empty ``sample_evals`` is legal — the loop simply doesn't run. Returns the
    first conformance error or ``None``.
    """
    sample_evals = generated.sample_evals or []
    if not sample_evals:
        return None

    # Local import — keeps the cold preview path fast; jsonschema is only
    # imported when there are rows to check.
    from jsonschema import Draft202012Validator  # noqa: PLC0415
    from jsonschema import ValidationError as JSONSchemaValidationError  # noqa: PLC0415

    try:
        input_validator = Draft202012Validator(generated.input_schema)
        output_validator = Draft202012Validator(generated.output_schema)
    except Exception as exc:
        # A malformed schema is retry-able, not a crash — broad except so the
        # retry loop can re-prompt the model to fix it.
        return f"sample_evals validation could not build schema validators: {exc}"

    for index, row in enumerate(sample_evals):
        if not isinstance(row, dict):
            return f"sample_evals[{index}] is not an object with 'input'/'expected' keys"
        if "input" not in row:
            return f"sample_evals[{index}] is missing the 'input' key"
        if "expected" not in row:
            return f"sample_evals[{index}] is missing the 'expected' key"
        try:
            input_validator.validate(row["input"])
        except JSONSchemaValidationError as exc:
            return f"sample_evals[{index}].input does not match input_schema: {exc.message}"
        try:
            output_validator.validate(row["expected"])
        except JSONSchemaValidationError as exc:
            return f"sample_evals[{index}].expected does not match output_schema: {exc.message}"
    return None


def _try_validate(
    generated: GeneratedAgent,
    *,
    name: str,
    skill_provisioner: SkillProvisioner | None = None,
) -> str | None:
    """Write ``generated`` to a tempdir + ``load_agent`` it + cross-check
    ADR 023 retrieval + cross-check ``sample_evals`` rows against the
    generated schemas. Returns ``None`` on success, or the first error string.

    The same three-layer validation the CLI's retry loop runs:

    1. :func:`load_agent` — proves the agent.yaml + prompt + schemas form a
       loadable, runnable bundle.
    2. :func:`check_adr023_retrieval` — catches RAG-shape misconfigurations
       at scaffold time rather than at ``mdk validate`` time.
    3. :func:`_validate_sample_evals` — every present row's ``input`` matches
       ``input_schema`` and ``expected`` matches ``output_schema``.

    A RAG-shape scaffold that passes here passes ``mdk validate`` by
    construction (same helpers).

    ``skill_provisioner`` is an optional CLI-side hook (F3, #112): a RAG-shape
    scaffold declares the built-in ``kb-vector-lookup`` skill, which
    ``load_agent`` resolves against the project's ``skills/`` registry. The
    CLI passes a callable that copies the curated packaged skill into the
    tempdir's project root so the validation tempdir mirrors what the
    committed project will look like. The runtime endpoint passes ``None``
    (it's read-only — no project to provision into); a candidate declaring an
    unresolvable skill fails cleanly there, which is the right answer.
    """
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp_agent_dir = Path(raw_tmp) / name
        try:
            write_agent_files(generated, target_dir=tmp_agent_dir)
        except (OSError, ValueError) as exc:
            return f"file write failed: {exc}"
        # Optional skill provisioning — the CLI uses this to materialize a
        # RAG-shape scaffold's declared built-in skill alongside the candidate
        # so ``load_agent`` resolves it. The runtime preview endpoint passes
        # ``None`` and skips this step.
        if skill_provisioner is not None:
            try:
                skill_provisioner(generated, Path(raw_tmp))
            except Exception as exc:
                # A hook failure is a validation error (callable misbehaving),
                # not a crash — broad except so the retry loop sees it.
                return f"skill provisioning failed: {exc}"
        try:
            bundle = load_agent(tmp_agent_dir)
        except AgentLoadError as exc:
            return str(exc)
        # ADR 023 cross-check — same helper ``mdk validate`` uses.
        adr023_error = check_adr023_retrieval(bundle)
        if adr023_error is not None:
            return adr023_error

    eval_error = _validate_sample_evals(generated)
    if eval_error is not None:
        return eval_error
    return None


def _safe_cost(*, model: str, tokens: TokenUsage) -> float | None:
    """Compute cost in USD; return ``None`` if the model isn't in the pricing
    table or the lookup fails for any other reason.

    A preview should never abort on a pricing-table miss — the candidate is
    still useful and we just skip the cost line.
    """
    from movate.providers.pricing import load_pricing  # noqa: PLC0415

    try:
        pricing = load_pricing()
        return pricing.cost_for(provider=model, tokens=tokens)
    except (KeyError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# Single retry on a load-validation failure — same policy the CLI uses
# (``cli/init.py``). One retry catches most "model emitted slightly wrong
# JSON" cases without doubling the cost of every preview.
_MAX_ATTEMPTS = 2


async def preview_agent_from_description(
    *,
    description: str,
    name: str,
    provider: BaseLLMProvider,
    model: str,
    target_model: str | None = None,
    timeout_seconds: float | None = None,
    progress: ProgressCallback | None = None,
    skill_provisioner: SkillProvisioner | None = None,
) -> ScaffoldPreview:
    """Generate + validate an agent preview from a natural-language description.

    The pure preview path shared by ``mdk init --llm`` and
    ``POST /api/v1/agents/preview`` (ADR 032 D1). Calls the LLM through the
    shipped :class:`BaseLLMProvider` seam (no new provider, no new dep), runs
    the same validation the CLI runs (load + ADR 023 + sample_evals schema),
    and returns a structured :class:`ScaffoldPreview` (or raises
    :class:`ScaffoldPreviewError`).

    Read-only: nothing is written to a persistent destination. The validation
    pass uses :class:`tempfile.TemporaryDirectory` purely to prove the candidate
    round-trips through :func:`load_agent`.

    Args:
        description: Natural-language description of the agent.
        name: Slug the agent will be saved under (validated by the caller).
        provider: The :class:`BaseLLMProvider` that drives the generation
            call. ``MockProvider()`` for offline / no-key tests; a
            ``LiteLLMProvider()`` for real provider calls. The caller wires
            this — this module never instantiates a provider.
        model: LiteLLM-style model id that DRIVES the scaffold call (e.g.
            ``openai/gpt-4o-mini-2024-07-18``).
        target_model: Optional model string to embed in the GENERATED
            agent.yaml's ``model.provider``. Defaults to ``model`` when
            unset.
        timeout_seconds: Optional overall timeout. ``None`` disables the
            guard; pass a finite value (e.g. 60.0) when calling from an HTTP
            request handler to bound request duration.
        progress: Optional :class:`ProgressCallback` invoked at each lifecycle
            event (``ATTEMPT_STARTED``, ``GENERATION_FAILED``,
            ``VALIDATION_FAILED``, ``ATTEMPT_RETRY_STARTED``). The CLI uses
            this to drive its spinner / between-attempt warning; the runtime
            endpoint passes ``None`` and the events are silently dropped.
        skill_provisioner: Optional :class:`SkillProvisioner` hook the CLI
            uses to materialize built-in / tool-use stubbed skills alongside
            the validation tempdir's project root so ``load_agent`` resolves
            them. The runtime endpoint passes ``None`` (read-only preview;
            no project to provision into) and a RAG-shape candidate
            declaring an unresolvable skill fails validation cleanly.

    Raises:
        ScaffoldPreviewError: typed failure with mode + partial state.
    """
    # Empty / whitespace-only description → fail fast before we spend any
    # tokens. The CLI does the same guard at the Typer handler; we mirror it
    # here so the runtime endpoint surfaces the same shape.
    if not description.strip():
        raise ScaffoldPreviewError(
            mode=PreviewFailureMode.EMPTY_DESCRIPTION,
            message="description is empty",
        )

    chosen_target_model = target_model or model

    if timeout_seconds is not None:
        return await asyncio.wait_for(
            _run_pipeline(
                description=description,
                name=name,
                provider=provider,
                model=model,
                target_model=chosen_target_model,
                progress=progress,
                skill_provisioner=skill_provisioner,
            ),
            timeout=timeout_seconds,
        )
    return await _run_pipeline(
        description=description,
        name=name,
        provider=provider,
        model=model,
        target_model=chosen_target_model,
        progress=progress,
        skill_provisioner=skill_provisioner,
    )


def _emit(
    progress: ProgressCallback | None,
    event: PreviewProgressEvent,
    message: str | None,
) -> None:
    """Best-effort observer dispatch — never raises into the pipeline.

    A misbehaving callback (CLI Rich console error, anything) must not abort
    the preview. Swallow the exception; the callback is purely cosmetic.
    """
    if progress is None:
        return
    with contextlib.suppress(Exception):
        progress(event, message)


async def _run_pipeline(
    *,
    description: str,
    name: str,
    provider: BaseLLMProvider,
    model: str,
    target_model: str,
    progress: ProgressCallback | None,
    skill_provisioner: SkillProvisioner | None,
) -> ScaffoldPreview:
    """Inner generation+retry+validation loop. Split out so the public
    function can wrap it in ``asyncio.wait_for`` cleanly.

    Mirrors the CLI's loop (``cli/init.py::_run_llm_scaffold``):

    * Attempt 1: generate → (defensive coercions) → canonical-defaults →
      validate.
    * If validation failed: feed the parsed-but-invalid attempt + error back
      to the model, retry once.
    * If generation itself failed (LLMScaffoldError): retry once (no feedback
      to feed back — fresh roll).
    * Either failure mode on the final attempt raises
      :class:`ScaffoldPreviewError`.
    """
    # Lazy import so test monkeypatches of
    # ``movate.scaffold.generate_agent_from_description`` take effect at this
    # call site (CLAUDE.md rule 11: treat generated code as a draft + rule 9:
    # tests first). A module-level binding would freeze the pre-patch function.
    from movate.scaffold import generate_agent_from_description  # noqa: PLC0415

    total_tokens = TokenUsage()
    retried = False

    feedback_attempt: GeneratedAgent | None = None
    feedback_error: str | None = None
    last_gen_error: str | None = None
    last_validation_error: str | None = None

    generated: GeneratedAgent | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        is_retry = attempt > 1
        if is_retry:
            retried = True

        _emit(
            progress,
            PreviewProgressEvent.ATTEMPT_RETRY_STARTED
            if is_retry
            else PreviewProgressEvent.ATTEMPT_STARTED,
            None,
        )

        try:
            result = await generate_agent_from_description(
                description=description,
                name=name,
                model=model,
                target_model=target_model,
                provider=provider,
                previous_attempt=feedback_attempt,
                validation_error=feedback_error,
            )
        except LLMScaffoldError as exc:
            last_gen_error = str(exc)
            last_validation_error = None
            feedback_attempt = None
            feedback_error = None
            if is_retry:
                break  # both attempts exhausted; raise below
            _emit(progress, PreviewProgressEvent.GENERATION_FAILED, last_gen_error)
            continue

        total_tokens = _accumulate_tokens(total_tokens, result.tokens)
        candidate = result.agent

        # Defensive coercions identical to the CLI's: a forgetful LLM might
        # echo an exemplar's name, or set the wrong model.provider for the
        # caller's key. Force the canonical answer.
        candidate.agent_yaml["name"] = name
        model_block = candidate.agent_yaml.get("model")
        if isinstance(model_block, dict):
            model_block["provider"] = target_model

        # Align the candidate's operational field set with a hand-init'd agent.
        apply_canonical_agent_defaults(candidate.agent_yaml, target_model=target_model)

        # Layered validation — same as the CLI.
        validation_error = _try_validate(candidate, name=name, skill_provisioner=skill_provisioner)
        if validation_error is None:
            generated = candidate
            last_gen_error = None
            last_validation_error = None
            break  # success

        # Validation failed → stash for the feedback retry (or final fail).
        last_validation_error = validation_error
        last_gen_error = None
        feedback_attempt = candidate
        feedback_error = validation_error
        if is_retry:
            break
        _emit(progress, PreviewProgressEvent.VALIDATION_FAILED, validation_error)

    if generated is None:
        # Final failure — pick the mode based on what went wrong last.
        if last_gen_error is not None:
            raise ScaffoldPreviewError(
                mode=PreviewFailureMode.GENERATION,
                message=last_gen_error,
                tokens=total_tokens,
                partial_agent=None,
                retried=retried,
            )
        raise ScaffoldPreviewError(
            mode=PreviewFailureMode.VALIDATION,
            message=last_validation_error or "unknown validation error",
            tokens=total_tokens,
            partial_agent=feedback_attempt,
            retried=retried,
        )

    cost_usd = _safe_cost(model=model, tokens=total_tokens)
    return ScaffoldPreview(
        agent=generated,
        tokens=total_tokens,
        cost_usd=cost_usd,
        retried=retried,
        target_model=target_model,
    )
