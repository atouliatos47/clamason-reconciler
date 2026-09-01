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

    -- OEE, from the SFC weekly exports via parsers.oee_parser.
    -- Every figure here is recomputed from summed raw hours and part
    -- counts, never averaged from SFC's own weekly percentages — on
    -- June 2026 those two methods differ by 17.8 points (36.64 vs 54.41).
    --
    -- oee_week_count is a leftover from the old weekly-upload workflow
    -- (SFC weeks run Sun-Sun and never align with month ends, so a
    -- "month" used to be however many weekly files got uploaded).
    -- Since the move to SFC's own monthly export, this is always 1 —
    -- kept rather than dropped so historical rows from the weekly era
    -- still read correctly, but nothing new should treat it as
    -- meaningful; oee_machine_count is the field that still varies.
    --
    -- oee_performance_pct is the CAPPED value used in the OEE product;
    -- oee_performance_pct_raw preserves the uncapped figure, which
    -- exceeds 100% when a machine's ideal-parts setting in SFC is wrong.
    oee_pct                 NUMERIC,
    oee_availability_pct    NUMERIC,
    oee_performance_pct     NUMERIC,
    oee_performance_pct_raw NUMERIC,
    oee_quality_pct         NUMERIC,
    oee_week_count          INTEGER,
    oee_machine_count       INTEGER,
    oee_run_hrs             NUMERIC,
    oee_net_avail_hrs       NUMERIC,
    oee_total_avail_hrs     NUMERIC,   -- calendar hours (24/7) — TEEP's denominator, OEE's isn't
    oee_utilization_pct     NUMERIC,   -- net_avail / total_avail — the factor OEE leaves out
    oee_teep_pct            NUMERIC,   -- oee_pct * utilization_pct / 100
    oee_total_parts         NUMERIC,
    oee_scrap_parts         NUMERIC,
    oee_per_machine         JSONB,      -- full per-machine detail, for the board review

    -- Reliability, from the Agility MTBF export via parsers.mtbf_parser.
    -- These are the PLANT-ONLY scope. That export has no craft column
    -- and lists presses, plant and tools together; on June 2026 tools are
    -- 3,279h of the 3,469h total, so the unfiltered MTTR (18.96h) is a
    -- toolroom figure, not a maintenance one. Plant-only gives 1.41h.
    -- All three scopes are kept in mtbf_scopes for audit.
    --
    -- mtbf_assets vs mtbf_asset_count is the honesty pair: Agility can
    -- only calculate MTBF for assets with more than one job, so June's
    -- figure rests on 4 of 12 plant assets. Storing both means the UI
    -- can say "5.77 days across 4 of 12" instead of implying fleet-wide.
    mtbf_days               NUMERIC,
    mtbf_assets             INTEGER,    -- assets with a real MTBF
    mtbf_asset_count        INTEGER,    -- assets in scope
    mtbf_mttr_hrs           NUMERIC,
    mtbf_wait_hrs           NUMERIC,
    mtbf_jobs               INTEGER,
    mtbf_downtime_hrs       NUMERIC,
    mtbf_scopes             JSONB,      -- {all, plant, tools} summaries

    -- Toolmaker-craft work orders, from the same Agility export as the
    -- maintenance WOs but a separate craft pass. The board review's
    -- Toolroom card used to show the maintenance count under a 'tool
    -- WOs' label; these are the real figures.
    -- total INCLUDES cancelled jobs (the card reads 'WOs raised'), with
    -- completed and cancelled stored alongside so the note can qualify it.
    toolroom_wo_count       INTEGER,
    toolroom_wo_completed   INTEGER,
    toolroom_wo_cancelled   INTEGER,
    toolroom_wo_open        INTEGER,
    -- TPM/PPM Schedule Completion — the board's own established KPI
    -- (see parsers/wo_parser.py summarise_ppm_completion), never
    -- previously surfaced by this reconciler. total/completed count
    -- Maintenance/Electrician jobs in config.PLANNED_JOB_TYPES_DAILY
    -- only, not every WO raised — a breakdown or a project isn't a
    -- missed PPM, so it must never drag this figure down.
    ppm_total                INTEGER,
    ppm_completed            INTEGER,
    ppm_completion_pct       NUMERIC,
    oee_quality_source      TEXT,
    efacs_scrap_qty         NUMERIC,
    efacs_scrap_cost        NUMERIC,
    -- TEEP against config.SHIFT_HOURS_PER_WEEK (the intended roster)
    -- instead of the full 24/7 calendar oee_teep_pct above uses. NULL
    -- fleet-wide until at least one machine has a real (non-placeholder)
    -- entry in that config table — see config.py for why it starts
    -- empty rather than guessed.
    oee_intended_hours               NUMERIC,
    oee_utilization_vs_intended_pct  NUMERIC,
    oee_teep_vs_intended_pct         NUMERIC,
    oee_intended_configured_count    INTEGER,

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

-- Same reason as the block above: monthly_runs already exists in the live
-- database, so the OEE and MTBF columns need ALTERs as well as being in
-- the CREATE. Existing rows get NULL, which is correct — those months were
-- checked before OEE and MTBF could be uploaded.
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_pct                 NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_availability_pct    NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_performance_pct     NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_performance_pct_raw NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_quality_pct         NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_week_count          INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_machine_count       INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_run_hrs             NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_net_avail_hrs       NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_total_avail_hrs     NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_utilization_pct     NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_teep_pct            NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_total_parts         NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_scrap_parts         NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_per_machine         JSONB;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mtbf_days               NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mtbf_assets             INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mtbf_asset_count        INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mtbf_mttr_hrs           NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mtbf_wait_hrs           NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mtbf_jobs               INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mtbf_downtime_hrs       NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS mtbf_scopes             JSONB;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS toolroom_wo_count       INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS toolroom_wo_completed   INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS toolroom_wo_cancelled   INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS toolroom_wo_open        INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS ppm_total                INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS ppm_completed            INTEGER;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS ppm_completion_pct       NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_quality_source      TEXT;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS efacs_scrap_qty         NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS efacs_scrap_cost        NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_intended_hours               NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_utilization_vs_intended_pct  NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_teep_vs_intended_pct         NUMERIC;
ALTER TABLE monthly_runs ADD COLUMN IF NOT EXISTS oee_intended_configured_count    INTEGER;


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

-- One day's Daily UK OEE By Machine Tabular export. Deliberately stores
-- raw summable hours/parts, not oee_pct/availability_pct/etc directly —
-- OEE percentages can't be averaged across days any more than they can
-- across weeks (see oee_parser.py's own long-standing warning on this,
-- and the 17.8-point gap it documents between the two methods on real
-- data). A weekly or monthly OEE Trend view needs to SUM these raw
-- fields first and only then compute a ratio, exactly like every other
-- OEE figure in this app already does — storing the ratio instead would
-- make that impossible to do correctly later.
--
-- per_machine is the one exception: a per-press breakdown is only ever
-- shown for a single selected day, never summed across days, so there's
-- no equivalent trap in storing it pre-computed.
CREATE TABLE IF NOT EXISTS oee_daily_snapshots (
    id                      SERIAL PRIMARY KEY,
    date                    DATE NOT NULL,
    period                  TEXT,
    machine_count           INTEGER,
    total_avail_hrs         NUMERIC,
    planned_down_hrs        NUMERIC,
    net_avail_hrs           NUMERIC,
    unplanned_down_hrs      NUMERIC,
    run_time_hrs            NUMERIC,
    total_parts             NUMERIC,
    ideal_parts             NUMERIC,
    scrap_parts             NUMERIC,
    per_machine             JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_oee_daily_snapshots_date ON oee_daily_snapshots (date);

-- Matt's WK30-style production plan workbook, imported once a week.
-- One row per week, not per day — the source is already a weekly
-- figure, so there's no daily granularity to roll up the way OEE
-- snapshots are. plan_quantity/plan_hours are straight sums of the
-- Planned and Available Run Hrs columns — see
-- parsers/production_plan_xlsx.py for why those two specifically,
-- not the sheet's own (broken) Hours Required column.
CREATE TABLE IF NOT EXISTS production_plan_weekly (
    id                      SERIAL PRIMARY KEY,
    week_start              DATE NOT NULL,
    source_filename         TEXT,
    sheet_name              TEXT,
    plan_quantity           NUMERIC,
    plan_hours              NUMERIC,
    row_count               INTEGER,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_production_plan_weekly_week_start ON production_plan_weekly (week_start);

-- One free-text root-cause note per department (Production/Toolroom/
-- Maintenance), living on that department's own page next to whichever
-- downtime reason currently has the most hours. This used to hold the
-- whole notes-and-actions blob as one shared string — the structure
-- wasn't settled yet at the time. It's settled now: root cause stays
-- here as free text (it's genuinely prose, no natural fields to split
-- it into), and the actions themselves moved out to their own table,
-- department_actions, below — each one needs its own target date,
-- which a single text blob can't hold per-line. updated_by is free
-- text, not a real user system — there's no login on this app, so
-- it's whatever name someone types in, purely for "who last touched
-- this" context, not access control.
CREATE TABLE IF NOT EXISTS department_notes (
    id                      SERIAL PRIMARY KEY,
    department              TEXT NOT NULL,
    notes                   TEXT,
    updated_by              TEXT,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_department_notes_dept ON department_notes (department);

-- One row per action item under a department's action plan. Kept
-- deliberately dumb: no owner/status enum, no separate audit trail —
-- just the text, an optional target date, and a done flag, which is
-- everything the mockup asked for. sort_order preserves the order
-- someone typed the actions in (Postgres doesn't guarantee row order
-- on its own), and is rewritten 0..n on every save alongside the rest
-- of the row — see save_department_actions in db.py, which replaces
-- a department's whole action list in one go rather than trying to
-- diff and patch individual rows.
CREATE TABLE IF NOT EXISTS department_actions (
    id                      SERIAL PRIMARY KEY,
    department              TEXT NOT NULL,
    action_text             TEXT NOT NULL,
    target_date             DATE,
    done                    BOOLEAN NOT NULL DEFAULT false,
    sort_order              INTEGER NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_department_actions_dept ON department_actions (department, sort_order);
