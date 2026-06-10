"""Certification suite entrypoint — ``uv run python -m certification.run_suite``.

Runs every scenario's ``cases.yaml`` against a deployed runtime (``--target
dev``), prints the scenario x capability matrix, and exits non-zero on any
failure. See ``certification/README.md`` for the capability contract.

Metrics: :func:`movate.tracing.metrics.init_metrics` is called at startup so
each ``certify`` block emits ``mdk.certification.scenario`` — but the OTLP
exporter only activates when the standard ``OTEL_EXPORTER_OTLP_*`` env is set
(fail-soft no-op otherwise). From a laptop the internal collector is not
reachable, so the Grafana matrix dashboard only fills when the suite runs
in-env (follow-up: an ACA job). The printed matrix + exit code are the local
source of truth either way.

Exit codes: 0 = no capability failed; 1 = at least one failure;
2 = configuration/usage error (missing key, unknown scenario, local mode).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from certification.harness.driver import (
    CaseResult,
    CaseSpecError,
    RuntimeApiClient,
    ScenarioSpec,
    SuiteDriver,
    aggregate_matrix,
    load_scenario_spec,
    render_matrix,
    side_effects_db_configured,
    summary_json,
)
from movate.tracing.metrics import init_metrics

DEFAULT_DEV_API_URL = "https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io"
_SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


def _discover_scenarios(filter_name: str | None) -> list[ScenarioSpec]:
    specs: list[ScenarioSpec] = []
    for cases_file in sorted(_SCENARIOS_DIR.glob("*/cases.yaml")):
        spec = load_scenario_spec(cases_file)
        if filter_name is None or spec.scenario == filter_name:
            specs.append(spec)
    return specs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="certification.run_suite",
        description="Run the MDK certification suite against a deployed runtime.",
    )
    parser.add_argument(
        "--target",
        choices=("dev", "local"),
        default="dev",
        help="dev = the deployed runtime API (default); local is deferred for now",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help="run only the scenario with this name (default: all discovered)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MDK_DEV_API_URL", DEFAULT_DEV_API_URL),
        help="runtime API base URL (env MDK_DEV_API_URL overrides the default)",
    )
    parser.add_argument(
        "--fact-timeout",
        type=float,
        default=180.0,
        help="seconds to wait for a terminal observability fact per case (default 180)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the machine-readable summary instead"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.target == "local":
        # Deliberately deferred: a faithful local mode needs a worker + Temporal
        # + the bundled workflow, which is its own piece of work. Fail loudly
        # rather than half-simulating it.
        print(
            "error: --target local is not implemented yet — the suite currently "
            "drives the deployed dev runtime. Run with --target dev and "
            "MDK_DEV_KEY set.",
            file=sys.stderr,
        )
        return 2

    api_key = os.environ.get("MDK_DEV_KEY", "").strip()
    if not api_key:
        print(
            "error: MDK_DEV_KEY is not set — export the dev runtime's bearer "
            "token, e.g. MDK_DEV_KEY=... uv run python -m certification.run_suite",
            file=sys.stderr,
        )
        return 2

    try:
        specs = _discover_scenarios(args.scenario)
    except CaseSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not specs:
        print(
            f"error: no scenario named {args.scenario!r} under {_SCENARIOS_DIR}",
            file=sys.stderr,
        )
        return 2

    # Fail-soft: emits mdk.certification.scenario only when OTLP env is set
    # (in-env runs); otherwise a silent no-op and the printed matrix rules.
    init_metrics()

    def _silent(_msg: str) -> None:
        return None

    log = _silent if args.json else print
    if not side_effects_db_configured():
        log(
            "note: MOVATE_PG_URL/MOVATE_DB_URL not set — sim-ledger (side-effects) "
            "expectations will be SKIPPED, not asserted."
        )

    scenario_results: list[tuple[ScenarioSpec, list[CaseResult]]] = []
    with RuntimeApiClient(args.base_url, api_key) as client:
        driver = SuiteDriver(client, fact_timeout_s=args.fact_timeout)
        for spec in specs:
            results = driver.run_scenario(spec, log=log)
            scenario_results.append((spec, results))

    summary = summary_json(args.target, scenario_results)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        rows = [(spec.scenario, aggregate_matrix(results)) for spec, results in scenario_results]
        print()
        print(render_matrix(rows))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
