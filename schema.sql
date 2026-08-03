-- Clamason Maintenance Reconciler — full schema.
--
-- db.py reads and writes THREE tables. Until now only monthly_runs was
-- defined here, so init_schema() silently created one table and left the
-- other two missing — every save_daily_snapshot() / save_sfc_daily_snapshot()
-- call failed with 'relation does not exist'. All three now live here so
-- init_db.py creates a complete database in one run.
--
-- Every table is keyed on its natural period (period / date) with a UNIQUE
-- index, because all three save functions use ON CONFLICT ... DO UPDATE:
-- re-uploading a corrected file for the same day or month overwrites that
-- row instead of creating a duplicate. Without the unique index the
-- ON CONFLICT clause fails at runtime, so these indexes are load-bearing,
-- not just tidy.


-- ---------------------------------------------------------------------------
-- monthly_runs — one row per period checked (e.g. one row for "June 2026").
-- machine_breakdown is JSONB so the dashboard can chart any machine's trend
-- over time without needing a separate table per machine.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monthly_runs (
    id                      SERIAL PRIMARY KEY,
    period                  TEXT NOT NULL,
    period_label            TEXT,
    machine_count           INTEGER,
    total_hrs               NUMERIC,
    total_events            INTEGER,
    maintenance_hrs         NUMERIC,
    toolroom_hrs            NUMERIC,
    agility_maintenance_hrs NUMERIC,
    gap_hrs                 NUMERIC,
    gap_pct                 NUMERIC,
    wo_count                INTEGER,
    machine_breakdown       JSONB,

    -- Repair times, from reconciliation.compute_repair_times(). Two sets
    -- because there are two defensible answers to "which assets count":
    --   _press = SFC-monitored presses only — the same asset set the gap
    --            figure describes, so MTTR and coverage % agree.
    --   _all   = every Maintenance/Electrician breakdown, including
    --            compressors, chillers and other plant the team maintains.
    -- On June 2026 data these differ by roughly 65%, so storing only one
    -- would bake a definitional choice into the history where nobody can
    -- see it. Both are kept; the board slide picks one and says which.
    --
    -- All NULLable: a month with no completed breakdown matched to a Down
    -- Time Analysis row has no MTTR, and NULL keeps it out of any trend
    -- average instead of a 0 pretending repairs took no time.
    mtta_hrs_press          NUMERIC,   -- Reported -> Started
    mttr_hrs_press          NUMERIC,   -- Started -> Finished (the real MTTR)
    mdt_hrs_press           NUMERIC,   -- Breakdown -> OnLine
    mttr_jobs_press         INTEGER,   -- denominator behind the means
    mtta_hrs_all            NUMERIC,
    mttr_hrs_all            NUMERIC,
    mdt_hrs_all             NUMERIC,
    mttr_jobs_all           INTEGER,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_monthly_runs_period ON monthly_runs (period);

-- The repair-time columns above are new, and monthly_runs ALREADY EXISTS in
-- the live database with real rows in it. CREATE TABLE IF NOT EXISTS does
-- NOT add columns to a table that's already there — it sees the table, does
-- nothing, and moves on. Without these ALTERs, init_db.py would report
-- success while save_run() failed on every call with
-- 'column "mtta_hrs_press" of relation "monthly_runs" does not exist'.
--
-- ADD COLUMN IF NOT EXISTS is safe to run repeatedly and never touches
-- existing rows: previously-saved months simply get NULL for the new
-- columns, which is correct — those months genuinely have no MTTR recorded.
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mtta_hrs_press  NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mttr_hrs_press  NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mdt_hrs_press   NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mttr_jobs_press INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mtta_hrs_all    NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mttr_hrs_all    NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mdt_hrs_all     NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mttr_jobs_all   INTEGER;


-- ---------------------------------------------------------------------------
-- daily_snapshots — one row per day of the Agility-side Daily View
-- (daily.compute_daily_summary output). Written by db.save_daily_snapshot(),
-- read by db.get_daily_snapshots() for the Daily Trend rollups.
--
-- mttr_hrs is deliberately NULLABLE: on a day with no COMPLETED maintenance
-- breakdown matched to a Down Time Analysis row, MTTR genuinely has no value.
-- Storing NULL rather than 0 keeps daily_trend.py's weighted average honest —
-- a zero would drag the weekly mean down as if repairs took no time.
-- mttr_matched / mttr_unmatched record how many breakdowns fed that average,
-- which is what makes the weighted rollup in daily_trend.py possible.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_snapshots (
    id                      SERIAL PRIMARY KEY,
    date                    DATE NOT NULL,
    total_wos               INTEGER,
    press_machine_wos       INTEGER,
    sitewide_wos            INTEGER,
    breakdowns_total        INTEGER,
    breakdowns_completed    INTEGER,
    planned_total           INTEGER,
    planned_completed       INTEGER,
    project_ci_total        INTEGER,
    project_ci_completed    INTEGER,
    other_total             INTEGER,
    mttr_hrs                NUMERIC,
    mttr_matched            INTEGER,
    mttr_unmatched          INTEGER,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_snapshots_date ON daily_snapshots (date);


-- ---------------------------------------------------------------------------
-- sfc_daily_snapshots — one row per day of the SFC Daily Downtime Summary PDF
-- (parsers.sfc_daily_downtime_pdf output). Written by
-- db.save_sfc_daily_snapshot(), read by db.get_sfc_daily_snapshots().
--
-- Kept entirely separate from daily_snapshots on purpose: that table is
-- Agility work-order counts, this one is SFC downtime hours. They come from
-- different systems, on different days, and either can be uploaded without
-- the other — merging them into one table would force a fake dependency
-- between two independent uploads.
--
-- `period` is SFC's own "Report Period" string, stored as-is for audit.
-- `date` is the day YOU assign at save time and is what everything keys on;
-- the two are deliberately not derived from each other, matching the
-- save_daily_snapshot() convention.
--
-- reasons / reason_events are JSONB ({reason: hrs} and {reason: count}) so a
-- Pareto for any day can be rebuilt from the saved row without re-uploading
-- the PDF. psycopg2 adapts JSONB to plain dicts on read — no json.loads()
-- needed on the way out.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sfc_daily_snapshots (
    id                      SERIAL PRIMARY KEY,
    date                    DATE NOT NULL,
    period                  TEXT,
    total_events            INTEGER,
    total_hrs               NUMERIC,
    maintenance_hrs         NUMERIC,
    toolroom_hrs            NUMERIC,
    production_hrs          NUMERIC,
    machine_count           INTEGER,
    period_hrs              NUMERIC,
    max_possible_hrs        NUMERIC,
    planned_offline_hrs     NUMERIC,
    scheduled_hrs           NUMERIC,
    reasons                 JSONB,
    reason_events           JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sfc_daily_snapshots_date ON sfc_daily_snapshots (date);
