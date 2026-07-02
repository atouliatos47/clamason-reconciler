-- Clamason Maintenance Reconciler — monthly runs table.
-- One row per period checked (e.g. one row for "June 2026").
-- machine_breakdown is stored as JSONB so the dashboard can chart any
-- machine's trend over time without needing a separate table per machine.

CREATE TABLE IF NOT EXISTS monthly_runs (
    id                      SERIAL PRIMARY KEY,
    period                  TEXT NOT NULL,          -- e.g. "6/1/2026 12:00:00 AM to 2026-07-01 00:00:00"
    period_label            TEXT,                    -- e.g. "June 2026" — human-friendly, set by the app
    machine_count           INTEGER,
    total_hrs               NUMERIC,
    total_events            INTEGER,
    maintenance_hrs         NUMERIC,                 -- SFC fault hours (FAULT-PRESS + FAULT-FEEDER/DECOILER/STR)
    toolroom_hrs            NUMERIC,
    agility_maintenance_hrs NUMERIC,                 -- hours actually matched to a real WO
    gap_hrs                 NUMERIC,
    gap_pct                 NUMERIC,
    wo_count                INTEGER,
    machine_breakdown       JSONB,                   -- full per-machine table: [{machine, fault_hrs, fault_events, wo_count, wo_hrs}, ...]
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One run per period — re-uploading the same month overwrites rather than duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS idx_monthly_runs_period ON monthly_runs (period);
