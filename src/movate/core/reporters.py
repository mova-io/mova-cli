"""Markdown reporters for ``EvalSummary`` and ``BenchSummary``.

Produces compact GitHub-Flavored Markdown blocks suitable for posting as a PR
comment from CI. The same renderers are also exposed via ``--output markdown``
on the corresponding CLI commands so a developer can preview locally.

Design choices:

* **Header verdict only** — full per-case details go inside a `<details>` block
  so the comment stays short by default but expands on click.
* **No external dependencies** — these renderers must work in environments
  where Rich isn't available (e.g. a stripped-down GitHub Action image).
* **Stable column order** so consumers can diff across runs without churn.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from movate.core.bench import BenchSummary, ModelBenchResult
    from movate.core.eval import EvalSummary


_MAX_INPUT_LEN = 60
_MAX_RATIONALE_LEN = 80


def render_eval_markdown(summary: EvalSummary, *, gate: float) -> str:
    """Render an ``EvalSummary`` as a GFM block.

    ``gate`` overrides ``summary.threshold`` for the per-case pass check —
    matches the CLI's behavior where ``--gate`` wins.
    """
    cases_passing = sum(1 for c in summary.cases if c.aggregated_score >= gate)
    overall_pass = summary.sample_count > 0 and cases_passing == summary.sample_count

    badge = "✅ PASS" if overall_pass else "❌ FAIL"
    icon = "🟢" if overall_pass else "🔴"

    lines: list[str] = []
    lines.append(f"### {icon} movate eval — `{summary.agent}` v{summary.agent_version}")
    lines.append("")
    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(f"| **Verdict** | {badge} |")
    lines.append(f"| Cases | {summary.sample_count} |")
    lines.append(f"| Mean score | {summary.mean_score:.3f} |")
    pass_pct = (cases_passing / summary.sample_count) if summary.sample_count else 0
    lines.append(f"| Pass rate | {cases_passing}/{summary.sample_count} ({pass_pct:.0%}) |")
    lines.append(f"| Gate | {gate:.2f} ({summary.gate_mode}) |")
    lines.append(f"| Runs/case | {summary.runs_per_case} |")
    if summary.judge_provider:
        jp = summary.judge_provider
        jp_display = f"{jp.split('+')[0]} +{len(jp.split('+')) - 1} more" if "+" in jp else jp
        judge_str = f"{summary.judge.method.value} ({jp_display})"
    else:
        judge_str = summary.judge.method.value
    lines.append(f"| Judge | {judge_str} |")
    lines.append(f"| Dataset hash | `{summary.dataset_hash[:12]}…` |")
    lines.append(f"| Total cost | ${summary.total_cost_usd:.6f} |")
    lines.append("")

    # Dimensional rollup — only when the dataset opted in to at least
    # one dataset-driven dim (faithfulness via ``grounding`` or coverage
    # via ``expected_coverage``). Latency is always scored on success
    # but on its own it's not interesting enough to render the section;
    # accuracy is already covered by the headline ``Mean score`` row.
    # Net: legacy datasets keep the exact v0.5 view, no extra noise.
    dm = summary.dimensional_means
    if any(v is not None for v in (dm.faithfulness, dm.coverage, dm.retrieval_accuracy)):
        lines.append("**Dimensional breakdown**")
        lines.append("")
        lines.append("| Dimension | Mean |")
        lines.append("|---|---|")
        for name, value in (
            ("accuracy", dm.accuracy),
            ("faithfulness", dm.faithfulness),
            ("retrieval_accuracy", dm.retrieval_accuracy),
            ("coverage", dm.coverage),
            ("latency", dm.latency),
        ):
            if value is None:
                continue
            lines.append(f"| {name} | {value:.3f} |")
        lines.append("")

    if summary.cases:
        lines.append(f"<details><summary>Per-case results ({summary.sample_count})</summary>")
        lines.append("")
        lines.append("| # | Score | Pass | Input | Rationale |")
        lines.append("|---|---|---|---|---|")
        for i, c in enumerate(summary.cases, start=1):
            passed_per_gate = c.aggregated_score >= gate
            check = "✅" if passed_per_gate else "❌"
            input_str = _md_code(json.dumps(c.case.input, ensure_ascii=False), _MAX_INPUT_LEN)
            rationale = c.runs[0].rationale if c.runs else ""
            lines.append(
                f"| {i} | {c.aggregated_score:.2f} | {check} | "
                f"{input_str} | {_md_escape(_truncate(rationale, _MAX_RATIONALE_LEN))} |"
            )
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


def render_bench_markdown(summary: BenchSummary) -> str:
    """Render a ``BenchSummary`` as a GFM block."""
    lines: list[str] = []
    lines.append(f"### movate bench — `{summary.agent}` v{summary.agent_version}")
    lines.append("")
    parts = [
        f"input: {_md_code(json.dumps(summary.input, ensure_ascii=False), _MAX_INPUT_LEN)}",
        f"runs/model: {summary.runs_per_model}",
    ]
    if summary.judge_provider:
        parts.append(f"judge: `{summary.judge_provider}`")
    parts.append(f"gate_mode: {summary.gate_mode}")
    lines.append(" · ".join(parts))
    lines.append("")

    has_score = any(m.aggregated_score(summary.gate_mode) is not None for m in summary.models)
    cols = ["Model", "Cost/run", "p50 ms", "p95 ms"]
    if has_score:
        cols.append("Score")
    cols.append("Errors")
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "|".join(["---"] * len(cols)) + "|")

    for m in summary.models:
        row = _bench_row(m, summary.gate_mode, has_score)
        lines.append("| " + " | ".join(row) + " |")

    skipped = [m.provider for m in summary.models if m.skipped_score]
    if skipped:
        lines.append("")
        lines.append(
            "> _judge skipped on same-family rows: " + ", ".join(f"`{p}`" for p in skipped) + "_"
        )

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _bench_row(m: ModelBenchResult, gate_mode: str, has_score: bool) -> list[str]:
    score = m.aggregated_score(gate_mode)
    cells = [
        f"`{m.provider}`",
        f"${m.cost_mean_usd:.6f}",
        str(m.latency_p50_ms),
        str(m.latency_p95_ms),
    ]
    if has_score:
        if m.skipped_score:
            score_cell = "_skipped_"
        elif score is None:
            score_cell = "—"
        else:
            score_cell = f"{score:.2f}"
        cells.append(score_cell)
    cells.append(str(m.error_count))
    return cells


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _md_escape(s: str) -> str:
    """Minimal escape: pipes break tables; backticks break inline code."""
    return s.replace("|", "\\|")


def _md_code(s: str, max_len: int) -> str:
    """Render an inline code span with truncation + pipe-escape.

    Backticks inside the value are replaced with their HTML entity so the
    span doesn't terminate early in the rendered comment.
    """
    truncated = _truncate(s, max_len)
    safe = truncated.replace("`", "&#96;").replace("|", "\\|")
    return f"`{safe}`"


__all__ = ["render_bench_markdown", "render_eval_markdown"]
