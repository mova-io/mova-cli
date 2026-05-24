"""Per-dimension eval drift (item 24, extends ADR 016 D2).

Continuous-eval drift originally compared only the aggregate ``mean_score`` /
``pass_rate`` between an eval and its baseline. A challenger can hold its
aggregate steady while ONE quality dimension (faithfulness, coverage, safety,
...) silently regresses. Item 24 persists per-dimension eval means
(``EvalRecord.dimension_means``) and teaches ``detect_drift`` to compare
per-dimension, so a single-dimension slide is caught.

Covers:

  * ``EvalSummary.to_record`` builds ``dimension_means`` from the per-case
    ``DimensionScores`` rollup (correct means; unscored dims omitted) while
    leaving ``mean_score`` / ``pass_rate`` byte-for-byte unchanged.
  * Storage round-trip (InMemory + sqlite; postgres behind a skip-guard),
    including old-record (None) compatibility.
  * The headline case: aggregate within tolerance but ONE dimension drops
    past it → ``regressed`` and the dimension is named; the inverse (all dims
    steady) → not regressed.
  * Back-compat guard: when either record has ``dimension_means=None``,
    ``detect_drift`` behaves exactly as the aggregate-only path.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from movate.core.drift import detect_drift
from movate.core.eval import (
    DimensionalMeans,
    DimensionScore,
    DimensionScores,
    EvalEngine,
)
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import EvalRecord, JudgeMethod
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.storage.sqlite import SqliteProvider
from movate.testing import (
    InMemoryStorage,
    NullTracer,
    scaffold_agent,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers (mirror test_eval.py / test_eval_dimensions.py)
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


def _executor(provider: MockProvider, pricing: PricingTable, storage, tracer) -> Executor:
    return Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)


def _eval(
    *,
    eval_id: str,
    agent: str = "demo",
    mean_score: float = 0.90,
    pass_rate: float = 0.90,
    dimension_means: dict[str, float] | None = None,
    dataset_hash: str = "h1",
    created_at: datetime | None = None,
    tenant_id: str = "tenant-a",
) -> EvalRecord:
    return EvalRecord(
        eval_id=eval_id,
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        dataset_hash=dataset_hash,
        judge_method=JudgeMethod.EXACT,
        judge_provider=None,
        runs_per_case=1,
        gate_mode="mean",
        threshold=0.7,
        mean_score=mean_score,
        pass_rate=pass_rate,
        sample_count=10,
        total_cost_usd=0.0,
        dimension_means=dimension_means,
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# DimensionalMeans.as_dict — unscored dims omitted, not stored as 0.0
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dimensional_means_as_dict_omits_unscored() -> None:
    means = DimensionalMeans(accuracy=0.9, faithfulness=0.8, safety=0.95)
    d = means.as_dict()
    assert d == {"accuracy": 0.9, "faithfulness": 0.8, "safety": 0.95}
    # Dims that were never scored (None) are absent — not 0.0.
    assert "coverage" not in d
    assert "latency" not in d


@pytest.mark.unit
def test_dimensional_means_as_dict_empty_when_nothing_scored() -> None:
    assert DimensionalMeans().as_dict() == {}


# ---------------------------------------------------------------------------
# EvalSummary.to_record — builds dimension_means; aggregate unchanged
# ---------------------------------------------------------------------------


def _scaffold(dst: Path, name: str = "demo") -> Path:
    return scaffold_agent(dst, name=name)


@pytest.mark.unit
async def test_to_record_populates_dimension_means(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """A coverage-bearing dataset yields a coverage mean on the record while
    mean_score / pass_rate stay byte-for-byte what the aggregate computes."""
    agent_dir = _scaffold(tmp_path / "demo")
    # Two cases, both with expected_coverage so the (deterministic) coverage
    # dim is scored. The MockProvider returns a fixed JSON output; coverage is
    # a substring check over it.
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "a"}, "expected": {"message": "A"}, '
        '"expected_coverage": ["hello"]}\n'
        '{"input": {"text": "b"}, "expected": {"message": "B"}, '
        '"expected_coverage": ["hello", "absent-topic"]}\n'
    )
    bundle = load_agent(agent_dir)
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)
    summary = await engine.run(bundle)

    record = summary.to_record()

    # Aggregate stays exactly what EvalSummary computes (byte-for-byte).
    assert record.mean_score == round(summary.mean_score, 6)
    assert record.pass_rate == round(summary.pass_rate, 6)

    # dimension_means is populated and matches the in-engine rollup projection.
    assert record.dimension_means is not None
    assert record.dimension_means == {
        name: round(value, 6) for name, value in summary.dimensional_means.as_dict().items()
    }
    # coverage was scored (1/1 then 1/2 → mean 0.75) — present + correct.
    assert record.dimension_means["coverage"] == pytest.approx(0.75)
    # A dimension no case scored (e.g. faithfulness, no grounding) is absent.
    assert "faithfulness" not in record.dimension_means


@pytest.mark.unit
async def test_to_record_dimension_means_none_for_exact_match(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """A plain exact-match dataset (no dimension scored beyond accuracy/latency)
    still gets a dict; if literally nothing is scored the field is None."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "a"}, "expected": {"message": "A"}}\n'
    )
    bundle = load_agent(agent_dir)
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)
    summary = await engine.run(bundle)
    record = summary.to_record()
    # Either None (nothing scored) or a dict that never contains a 0.0-padded
    # unscored dim — both are acceptable; the invariant is "no dim stored as a
    # placeholder". accuracy/latency are scored on the happy path.
    if record.dimension_means is not None:
        assert "faithfulness" not in record.dimension_means
        assert "coverage" not in record.dimension_means


# ---------------------------------------------------------------------------
# Storage round-trip — InMemory + sqlite + (guarded) postgres
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_inmemory_round_trip_with_and_without_dimension_means(storage) -> None:
    with_dims = _eval(eval_id="d1", dimension_means={"accuracy": 0.9, "safety": 0.95})
    legacy = _eval(eval_id="d2", dimension_means=None)
    await storage.save_eval(with_dims)
    await storage.save_eval(legacy)

    got1 = await storage.get_eval("d1", tenant_id="tenant-a")
    got2 = await storage.get_eval("d2", tenant_id="tenant-a")
    assert got1 is not None and got1.dimension_means == {"accuracy": 0.9, "safety": 0.95}
    assert got2 is not None and got2.dimension_means is None


@pytest.mark.unit
async def test_sqlite_round_trip_with_and_without_dimension_means(tmp_path: Path) -> None:
    provider = SqliteProvider(db_path=tmp_path / "drift.db")
    await provider.init()
    try:
        with_dims = _eval(eval_id="s1", dimension_means={"faithfulness": 0.81, "coverage": 0.6})
        legacy = _eval(eval_id="s2", dimension_means=None)
        await provider.save_eval(with_dims)
        await provider.save_eval(legacy)

        got1 = await provider.get_eval("s1", tenant_id="tenant-a")
        got2 = await provider.get_eval("s2", tenant_id="tenant-a")
        assert got1 is not None
        assert got1.dimension_means == {"faithfulness": 0.81, "coverage": 0.6}
        assert got2 is not None and got2.dimension_means is None

        # list_evals path round-trips the column too.
        listed = await provider.list_evals(tenant_id="tenant-a", agent="demo")
        by_id = {e.eval_id: e for e in listed}
        assert by_id["s1"].dimension_means == {"faithfulness": 0.81, "coverage": 0.6}
        assert by_id["s2"].dimension_means is None
    finally:
        await provider.close()


@pytest.mark.unit
async def test_sqlite_legacy_row_without_column_reads_none(tmp_path: Path) -> None:
    """A row inserted before the dimension_means migration reads back as None.

    Simulated by building the pre-item-24 evals table (15 columns, no
    dimension_means), inserting a row, then opening it through SqliteProvider
    (whose init() runs the additive ALTER). The old row must read None.
    """
    import aiosqlite  # noqa: PLC0415

    db_path = tmp_path / "legacy.db"
    legacy_schema = """
    CREATE TABLE evals (
        eval_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent TEXT NOT NULL,
        agent_version TEXT NOT NULL, dataset_hash TEXT NOT NULL,
        judge_method TEXT NOT NULL, judge_provider TEXT, runs_per_case INTEGER NOT NULL,
        gate_mode TEXT NOT NULL, threshold REAL NOT NULL, mean_score REAL NOT NULL,
        pass_rate REAL NOT NULL, sample_count INTEGER NOT NULL,
        total_cost_usd REAL NOT NULL, created_at TEXT NOT NULL
    );
    """
    conn = await aiosqlite.connect(db_path)
    await conn.executescript(legacy_schema)
    await conn.execute(
        "INSERT INTO evals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-1",
            "tenant-a",
            "demo",
            "0.1.0",
            "h1",
            "exact",
            None,
            1,
            "mean",
            0.7,
            0.9,
            0.9,
            10,
            0.0,
            datetime.now(UTC).isoformat(),
        ),
    )
    await conn.commit()
    await conn.close()

    provider = SqliteProvider(db_path=db_path)
    await provider.init()  # runs the additive ALTER TABLE evals ADD dimension_means
    try:
        got = await provider.get_eval("legacy-1", tenant_id="tenant-a")
        assert got is not None
        assert got.dimension_means is None
    finally:
        await provider.close()


@pytest.mark.smoke
async def test_postgres_round_trip_with_and_without_dimension_means() -> None:
    pg_url = os.environ.get("MOVATE_PG_TEST_URL")
    if not pg_url:
        pytest.skip("MOVATE_PG_TEST_URL not set — postgres round-trip skipped")
    from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

    provider = PostgresProvider(dsn=pg_url)
    await provider.init()
    try:
        with_dims = _eval(eval_id="pg1", dimension_means={"accuracy": 0.9, "safety": 0.95})
        legacy = _eval(eval_id="pg2", dimension_means=None)
        await provider.save_eval(with_dims)
        await provider.save_eval(legacy)
        got1 = await provider.get_eval("pg1", tenant_id="tenant-a")
        got2 = await provider.get_eval("pg2", tenant_id="tenant-a")
        assert got1 is not None and got1.dimension_means == {"accuracy": 0.9, "safety": 0.95}
        assert got2 is not None and got2.dimension_means is None
    finally:
        await provider.close()


# ---------------------------------------------------------------------------
# detect_drift — per-dimension regression (the headline case)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_aggregate_steady_but_one_dimension_regresses() -> None:
    """THE headline test: aggregate within tolerance, ONE dimension drops past
    it → regressed=True and the regressing dimension is named."""
    base = _eval(
        eval_id="b",
        mean_score=0.90,
        pass_rate=0.90,
        dimension_means={"accuracy": 0.95, "faithfulness": 0.90, "safety": 0.96},
    )
    cur = _eval(
        eval_id="c",
        mean_score=0.89,  # -0.01, within ±0.05
        pass_rate=0.90,  # steady
        dimension_means={"accuracy": 0.94, "faithfulness": 0.70, "safety": 0.95},
    )
    result = detect_drift(cur, base, tolerance=0.05)

    # Aggregate alone would NOT have regressed.
    assert "mean_score" not in result.regressed_metrics
    assert "pass_rate" not in result.regressed_metrics
    # But the faithfulness dimension dropped 0.20 → regression fires.
    assert result.regressed is True
    assert result.regressed_dimensions == ["faithfulness"]
    assert result.worst_dimension == "faithfulness"
    assert result.dimension_deltas["faithfulness"] == pytest.approx(-0.20)
    # summary() names the worst dimension.
    assert "faithfulness" in result.summary()
    assert "REGRESSION" in result.summary()


@pytest.mark.unit
def test_all_dimensions_steady_is_not_a_regression() -> None:
    """The inverse: aggregate steady AND every dimension steady → not
    regressed."""
    base = _eval(
        eval_id="b",
        mean_score=0.90,
        pass_rate=0.90,
        dimension_means={"accuracy": 0.95, "faithfulness": 0.90, "safety": 0.96},
    )
    cur = _eval(
        eval_id="c",
        mean_score=0.89,
        pass_rate=0.89,
        dimension_means={"accuracy": 0.94, "faithfulness": 0.88, "safety": 0.96},
    )
    result = detect_drift(cur, base, tolerance=0.05)
    assert result.regressed is False
    assert result.regressed_dimensions == []
    assert result.worst_dimension is None
    assert "worst dim" not in result.summary()


@pytest.mark.unit
def test_multiple_dimensions_regress_worst_first() -> None:
    base = _eval(
        eval_id="b",
        dimension_means={"faithfulness": 0.90, "coverage": 0.90, "safety": 0.96},
    )
    cur = _eval(
        eval_id="c",
        dimension_means={"faithfulness": 0.70, "coverage": 0.78, "safety": 0.96},
    )
    result = detect_drift(cur, base, tolerance=0.05)
    assert result.regressed is True
    # faithfulness (-0.20) is worse than coverage (-0.12) → sorted worst-first.
    assert result.regressed_dimensions == ["faithfulness", "coverage"]
    assert result.worst_dimension == "faithfulness"


@pytest.mark.unit
def test_dimension_improvement_is_never_a_regression() -> None:
    base = _eval(eval_id="b", dimension_means={"faithfulness": 0.70})
    cur = _eval(eval_id="c", dimension_means={"faithfulness": 0.95})
    result = detect_drift(cur, base, tolerance=0.05)
    assert result.regressed is False
    assert result.regressed_dimensions == []


@pytest.mark.unit
def test_dimension_only_one_side_scored_is_skipped() -> None:
    """A dimension present in only one record is not compared — a delta against
    a missing baseline is meaningless and an added dimension is not a
    regression."""
    base = _eval(eval_id="b", dimension_means={"accuracy": 0.95})
    cur = _eval(eval_id="c", dimension_means={"accuracy": 0.94, "safety": 0.10})
    result = detect_drift(cur, base, tolerance=0.05)
    # safety isn't shared → not compared; accuracy steady → no regression.
    assert result.regressed is False
    assert "safety" not in result.dimension_deltas
    assert set(result.dimension_deltas) == {"accuracy"}


# ---------------------------------------------------------------------------
# Back-compat guard — None dimension_means ⇒ aggregate-only, byte-for-byte
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_back_compat_both_none_is_aggregate_only() -> None:
    """When neither record carries dimension_means, detect_drift is exactly the
    pre-item-24 aggregate-only path."""
    base = _eval(eval_id="b", mean_score=0.90, pass_rate=0.90, dimension_means=None)
    cur = _eval(eval_id="c", mean_score=0.80, pass_rate=0.90, dimension_means=None)  # -0.10 mean
    result = detect_drift(cur, base, tolerance=0.05)
    assert result.regressed is True
    assert result.regressed_metrics == ["mean_score"]
    assert result.regressed_dimensions == []
    assert result.dimension_deltas == {}


@pytest.mark.unit
def test_back_compat_within_tolerance_none_dims_not_regressed() -> None:
    base = _eval(eval_id="b", mean_score=0.90, pass_rate=0.90, dimension_means=None)
    cur = _eval(eval_id="c", mean_score=0.87, pass_rate=0.88, dimension_means=None)
    result = detect_drift(cur, base, tolerance=0.05)
    assert result.regressed is False
    assert result.regressed_metrics == []
    assert result.regressed_dimensions == []


@pytest.mark.unit
def test_back_compat_one_side_none_falls_back_to_aggregate() -> None:
    """Even if ONE side carries dimension_means, a missing other side means no
    per-dimension comparison — the result is purely aggregate-driven."""
    # Current has a sharply regressed dim, but the baseline lacks the map →
    # the per-dim check can't run → only the (steady) aggregate is judged.
    base = _eval(eval_id="b", mean_score=0.90, pass_rate=0.90, dimension_means=None)
    cur = _eval(
        eval_id="c",
        mean_score=0.89,
        pass_rate=0.90,
        dimension_means={"faithfulness": 0.10},
    )
    result = detect_drift(cur, base, tolerance=0.05)
    assert result.regressed is False
    assert result.regressed_dimensions == []
    assert result.dimension_deltas == {}


@pytest.mark.unit
def test_back_compat_identical_to_pre_item24_for_aggregate_regression() -> None:
    """Construct the same scenario two ways — with and without dimension_means
    that are all steady — and assert the aggregate-driven verdict is identical.
    This guards that adding (steady) dimension data never changes a verdict the
    aggregate already produced."""
    # Aggregate-only (legacy) records.
    base_legacy = _eval(eval_id="b1", mean_score=0.90, pass_rate=0.90, dimension_means=None)
    cur_legacy = _eval(eval_id="c1", mean_score=0.80, pass_rate=0.90, dimension_means=None)
    legacy = detect_drift(cur_legacy, base_legacy, tolerance=0.05)

    # Same aggregate, plus steady dimension data (no dim regresses).
    steady_dims = {"accuracy": 0.9, "faithfulness": 0.9}
    base_dims = _eval(eval_id="b2", mean_score=0.90, pass_rate=0.90, dimension_means=steady_dims)
    cur_dims = _eval(eval_id="c2", mean_score=0.80, pass_rate=0.90, dimension_means=steady_dims)
    with_dims = detect_drift(cur_dims, base_dims, tolerance=0.05)

    assert legacy.regressed == with_dims.regressed is True
    assert legacy.regressed_metrics == with_dims.regressed_metrics == ["mean_score"]
    # The dimension data didn't add a regression (all steady).
    assert with_dims.regressed_dimensions == []


# ---------------------------------------------------------------------------
# _compute_dimensional_means rollup still omits unscored dims (item 24 reuse)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rollup_to_record_dict_skips_unscored() -> None:
    """A DimensionalMeans with mixed scored/None projects to a dict of only the
    scored dims — the exact contract to_record relies on."""
    means = DimensionalMeans(
        accuracy=0.8,
        coverage=0.6,
        faithfulness=None,  # unscored
        safety=None,  # unscored
    )
    assert means.as_dict() == {"accuracy": 0.8, "coverage": 0.6}


@pytest.mark.unit
def test_dimension_scores_namespace_shape_unaffected() -> None:
    """Sanity: the DimensionScores container the rollup walks is unchanged."""
    ds = DimensionScores(accuracy=DimensionScore(1.0, ""))
    ns = SimpleNamespace(runs=[SimpleNamespace(dimensions=ds)])
    assert ns.runs[0].dimensions.accuracy.value == 1.0
