-- ADR 095 — business-of-record stand-in for the local unified-observability demo.
--
-- In production this is the shared Azure Postgres `movate` DB (real run records,
-- cost ledger, governance). Here a minimal seed so the cross-store Grafana panel
-- is runnable out of the box: a `workflow_runs` table mirroring the mdk shape,
-- with a `trace_id` column — the join key (ADR 095 D4) that correlates the
-- authoritative cost/outcome here with the high-cardinality trace/span data in
-- ClickHouse.

CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_run_id TEXT PRIMARY KEY,
    workflow        TEXT NOT NULL,
    status          TEXT NOT NULL,           -- success | error | paused
    tier            TEXT,                    -- auto | manager | director (decision-node outcome)
    cost_usd        DOUBLE PRECISION NOT NULL DEFAULT 0,
    trace_id        TEXT NOT NULL,           -- the correlation key into ClickHouse otel_traces
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS governance_decisions (
    id          BIGSERIAL PRIMARY KEY,
    trace_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,               -- cost | quota | model | runtime
    effect      TEXT NOT NULL,               -- allow | warn | deny
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A small, realistic seed (the expense-approval template's tiers).
INSERT INTO workflow_runs (workflow_run_id, workflow, status, tier, cost_usd, trace_id, created_at) VALUES
  ('run-aa01', 'expense-approval', 'success', 'auto',     0.0012, 'trace-aa01', now() - interval '50 minutes'),
  ('run-aa02', 'expense-approval', 'success', 'manager',  0.0031, 'trace-aa02', now() - interval '42 minutes'),
  ('run-aa03', 'expense-approval', 'success', 'director', 0.0048, 'trace-aa03', now() - interval '33 minutes'),
  ('run-aa04', 'expense-approval', 'error',   'director', 0.0021, 'trace-aa04', now() - interval '20 minutes'),
  ('run-rf01', 'refund-approval',  'success', NULL,       0.0027, 'trace-rf01', now() - interval '15 minutes'),
  ('run-rf02', 'refund-approval',  'paused',  NULL,       0.0009, 'trace-rf02', now() - interval '4 minutes')
ON CONFLICT (workflow_run_id) DO NOTHING;

INSERT INTO governance_decisions (trace_id, kind, effect) VALUES
  ('trace-aa03', 'cost',  'warn'),
  ('trace-aa04', 'cost',  'deny'),
  ('trace-rf01', 'model', 'allow')
ON CONFLICT DO NOTHING;
